"""
Firefox testing utilities for BrowserPlug
Handles Firefox profile creation, extension configuration, and test coordination
"""

import os
import json
import hashlib
import tempfile
import shutil
import time
import subprocess
import sqlite3
import atexit
import tarfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# Set up consistent imports

from test_config import FIREFOX_TEST_CONFIG, get_firefox_test_port
from port_coordinator import FirefoxPortCoordinator, coordinated_mcp_port

project_root = Path(__file__).resolve().parent.parent

# The port the extension falls back to when it has no stored config — i.e. the
# developer's LIVE shared server (see CONFIG/loadConfig in extension-gecko/background.js).
# A test profile must never be able to reach it; see FirefoxTestManager's
# _BOOTSTRAP_NETWORK_BLOCK and _seed_extension_storage.
DEFAULT_EXTENSION_WS_PORT = 8772


def _exe_name(path):
    """The process name a browser binary runs under, without extension: 'firefox'."""
    return os.path.splitext(os.path.basename(os.path.expanduser(path or '')))[0].lower()


def _stop_firefox_tree(proc, profile_dir, exe_name, timeout=5):
    """Stop a test Firefox, its child processes, and anything else still running on its
    profile. Returns True if the parent had to be force-killed after `timeout`.

    Killing only the parent is not enough on Windows. Its content, GPU and socket children
    outlive it for a moment, holding files in the profile. And a first run can relaunch
    the browser as a new process, so the one we started may already be gone while the real
    browser keeps parent.lock: the profile then cannot be cached, and the next Firefox
    cannot use it. taskkill /T takes the tree down while the parent lives; the profile
    sweep catches whatever the tree no longer reaches."""
    forced = False
    if proc is not None:
        try:
            if proc.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    proc.terminate()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            forced = True
    _kill_profile_stragglers(profile_dir, exe_name, timeout=timeout)
    return forced


def _kill_profile_stragglers(profile_dir, exe_name, timeout=5):
    """Kill every `exe_name` process whose command line runs it on `profile_dir`.

    Matched by process name, a -profile flag and the profile's unique temp-dir name, so a
    browser the user runs themselves can never match."""
    if not profile_dir or not exe_name:
        return
    try:
        import psutil   # a declared server dependency, present in the test venv
    except ImportError:
        return
    marker = os.path.basename(os.path.normpath(profile_dir)).lower()
    victims = []
    for p in psutil.process_iter(['name']):
        if os.path.splitext(p.info.get('name') or '')[0].lower() != exe_name:
            continue
        try:
            cmd = [str(a).lower() for a in p.cmdline()]
        except psutil.Error:
            continue
        if ('-profile' in cmd or '--profile' in cmd) and any(marker in a for a in cmd):
            victims.append(p)
    for p in victims:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=timeout)


# Profile dirs cleanup() could not delete in time; the exit sweep below tries them again,
# once every Firefox this process started is long gone.
_UNREMOVED_PROFILE_DIRS = set()


def _remove_profile_dir(path, timeout=10.0):
    """Delete a test profile, retrying while Windows still holds its files.

    A Firefox that has just been stopped leaves files locked for a moment (a child process
    still closing, a virus scanner reading the cache it wrote), and a single failed rmtree
    leaks the whole profile into %TEMP% on every test. Returns False, and queues the dir
    for the exit sweep, if it still resists after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    delay = 0.1
    while True:
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError as e:
            if time.monotonic() >= deadline:
                print(f"⚠ Could not remove profile dir {path} ({e}); retrying at exit")
                _UNREMOVED_PROFILE_DIRS.add(path)
                return False
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


# Removals still running on their own thread; the exit sweep waits for them.
_PENDING_REMOVALS = []


def _remove_profile_dir_in_background(path):
    """Remove a stopped Firefox's profile without making the test wait a second or two
    for Windows to release its locks; the next test starts while the retries run."""
    _PENDING_REMOVALS[:] = [t for t in _PENDING_REMOVALS if t.is_alive()]
    worker = threading.Thread(target=_remove_profile_dir, args=(path,), daemon=True,
                              name=f"remove {os.path.basename(path)}")
    worker.start()
    _PENDING_REMOVALS.append(worker)


@atexit.register
def _remove_unremoved_profile_dirs():
    for worker in list(_PENDING_REMOVALS):
        worker.join(timeout=15)
    for path in list(_UNREMOVED_PROFILE_DIRS):
        shutil.rmtree(path, ignore_errors=True)
        _UNREMOVED_PROFILE_DIRS.discard(path)


# How long a process waits for another one to finish building a profile. A cold build runs
# Firefox to mint extensions.json and takes 10 to 25 s; five minutes means a hung build.
_PROFILE_CACHE_LOCK_TIMEOUT = 300


def _lock_byte(fh, lock):
    """Lock (or unlock) the first byte of `fh` without blocking; OSError when it is held."""
    if os.name == 'nt':
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK if lock else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if lock else fcntl.LOCK_UN)


@contextmanager
def _profile_cache_lock(cache_dir, timeout=_PROFILE_CACHE_LOCK_TIMEOUT):
    """Hold the cross-process lock on the profile cache while the block runs.

    Parallel pytest workers share dist/profile-cache. Unlocked, every worker that misses
    builds the same profile at once, and one worker's eviction can delete a tarball another
    is about to extract. The lock covers miss, build, store and prune. A restore takes no
    lock: it reads a tarball that is only ever swapped in whole (os.replace). The OS drops
    the lock when its process dies, so a killed run leaves none behind (the server's
    instance lock in server/runtime/lifecycle.py works the same way). Not reentrant: code
    inside the block must not take it again.
    """
    fh = open(os.path.join(cache_dir, '.lock'), 'a+')
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _lock_byte(fh, True)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"profile cache lock in {cache_dir} not acquired within {timeout}s: "
                        "another process is building a profile, or hung while building one")
                time.sleep(0.2)
        try:
            yield
        finally:
            _lock_byte(fh, False)
    finally:
        fh.close()


@dataclass
class ProfileCacheEntry:
    """Cache entry for Firefox profiles stored as compressed files"""
    key: str
    compressed_path: str  # Path to .tar.gz file
    created_at: float
    last_used: float
    use_count: int
    is_locked: bool = False

class FirefoxTestManager:
    """Manages Firefox instances for testing with proper extension configuration"""

    # Class-level cache for Firefox profiles.
    #
    # Keyed by what actually determines the profile's CONTENTS — the XPI's content hash,
    # the granted optional permissions, and the profile_tag — NOT by the port. Ports are
    # allocated dynamically per session (see port_coordinator), so a port-keyed cache
    # would miss on every run and rebuild the profile from scratch (~9-21s each). The
    # session's port is instead STAMPED INTO the restored profile (_apply_test_port),
    # which is possible because the port lives in exactly two writable places:
    # user.js and storage-sync-v2.sqlite.
    #
    # profile_tag keeps an EXTRA browser's profile distinct: a multi-browser test runs a
    # SECOND Firefox against the same server (same WS port) and it must get its OWN
    # profile — a distinct profile is what gives it a distinct extension instance_id,
    # i.e. a separate entry in the server's browser registry instead of displacing the
    # first browser's socket.
    _profile_cache = {}  # {cache_key: ProfileCacheEntry}
    _cache_dir = None
    _max_cache_size = 10
    # A tarball restored this recently may be in use by another process (a parallel
    # worker, or a run of an older build), so pruning leaves it alone. Every restore
    # refreshes the tarball's mtime, which makes the mtime "last used by anyone".
    _EVICTION_GRACE_SECONDS = 6 * 3600
    # A staging file this old belongs to a build that died before its os.replace.
    _STALE_STAGING_SECONDS = 3600
    _fingerprints = {}  # {(xpi_path, mtime, size, perms): fingerprint}

    # Optional permissions (manifest `optional_permissions`) the test profile pre-grants.
    # A fresh profile has them REVOKED, which makes the extension refuse the capability
    # (e.g. proxy routing) and the matching tests skip. Firefox reads granted optional
    # permissions from <profile>/extension-preferences.json at startup, so writing that
    # file is the headless equivalent of ticking the box in
    # about:addons ▸ BrowserPlug ▸ Permissions.
    _EXTENSION_ID = 'browserplug@marcinwolny.net'
    _GRANTED_OPTIONAL_PERMISSIONS = ['proxy']

    # --- Profile-bootstrap isolation -------------------------------------------------
    # Creating a profile runs Firefox a couple of times (to mint extensions.json) with
    # the extension already installed and ENABLED. Its background page immediately calls
    # loadConfig(), which reads browser.storage.sync — backed by <profile>/
    # storage-sync-v2.sqlite — and, finding nothing, falls back to the built-in default
    # ws://localhost:8772. That is the DEVELOPER'S live server: the bootstrap Firefox
    # takes over the server's single extension slot and answers MCP calls meant for the
    # real browser. Two independent guards, both live-verified on FF153:
    #
    #  1. _seed_extension_storage() puts the test port into storage-sync-v2.sqlite BEFORE
    #     Firefox's first start, so the extension never has a reason to use the default.
    #  2. This pref block makes the bootstrap Firefox unable to reach anything at all, so
    #     even a mis-seeded / rejected / future-schema storage DB cannot leak to :8772.
    #
    # Guard 2 is the fail-closed one and must stay: route EVERY request, loopback
    # included, through a SOCKS proxy that does not exist (127.0.0.1:1).
    #   * allow_hijacking_localhost is the load-bearing line — by default Firefox refuses
    #     to proxy loopback targets and would send ws://localhost:8772 straight out.
    #   * failover_direct=false stops a dead proxy from falling back to a direct connection.
    #   * The extension's own proxy.onRequest listener (proxyDecision -> {type:'direct'})
    #     does NOT override these prefs for its WebSocket, verified with the `proxy`
    #     permission granted. Note network.websocket.enabled=false is NOT used: it is a
    #     no-op in FF153 (the socket is still attempted), i.e. exactly the kind of
    #     safety-shaped nothing this block replaces.
    _BOOTSTRAP_NETWORK_BLOCK = '''
// --- bootstrap-phase network block (lifted once the profile is configured) ---
user_pref("network.proxy.type", 1);
user_pref("network.proxy.socks", "127.0.0.1");
user_pref("network.proxy.socks_port", 1);
user_pref("network.proxy.socks_version", 5);
user_pref("network.proxy.socks_remote_dns", true);
user_pref("network.proxy.allow_hijacking_localhost", true);
user_pref("network.proxy.failover_direct", false);
user_pref("network.proxy.no_proxies_on", "");
'''

    # Firefox persists user.js prefs into prefs.js, so merely dropping the lines above
    # would leave the block in force for the real test run. It has to be reversed
    # explicitly in the final user.js.
    _BOOTSTRAP_NETWORK_BLOCK_LIFTED = '''
// Bootstrap network block explicitly lifted (prefs.js keeps whatever user.js last set).
user_pref("network.proxy.type", 0);
user_pref("network.proxy.allow_hijacking_localhost", false);
'''

    # Schema of Firefox's storage.sync backing store (rust webext-storage, user_version 2).
    # loadConfig() in extension-gecko/background.js reads browser.storage.sync — NOT
    # storage.local, and NOT any hand-written JSON file in the profile.
    _STORAGE_SYNC_SCHEMA = '''
CREATE TABLE storage_sync_data (
    ext_id TEXT NOT NULL PRIMARY KEY,
    data TEXT,
    sync_change_counter INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE storage_sync_mirror (
    guid TEXT NOT NULL PRIMARY KEY,
    ext_id TEXT UNIQUE,
    data TEXT,
    CHECK((ext_id IS NULL AND data IS NULL) OR (ext_id IS NOT NULL AND data IS NOT NULL))
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value NOT NULL) WITHOUT ROWID;
PRAGMA user_version = 2;
'''

    def __init__(self, firefox_path=None, test_port=None, coordination_file=None,
                 profile_tag=None, mcp_port=None):
        """Manage one Firefox instance for tests.

        profile_tag: distinguishes an ADDITIONAL browser that shares the test server (and
            therefore the WS port) with another one — e.g. FirefoxTestManager(...,
            profile_tag='b') for the second Firefox of a multi-browser test. It only
            changes the profile-cache slot, so the two managers build/cache/reuse two
            SEPARATE profiles; everything else (isolation guards, seeded storage, XPI,
            granted optional permissions) is identical. Two profiles => two extension
            instance_ids => two entries in the server's browser registry.
            None (the default) keeps the historic behavior: one profile per port.
        mcp_port: the test server's MCP HTTP port, seeded as the extension's `mcpPort`
            setting so its first-run token bootstrap may fetch /ws-token from it (the
            extension refuses an endpoint on any other port outside 2830..2942). Read from
            the coordination file when that names one.
        """
        self.firefox_path = firefox_path or os.environ.get('FIREFOX_PATH', 'firefox')
        self.test_port = test_port or get_firefox_test_port()
        self.coordination_file = coordination_file
        self.profile_tag = profile_tag
        self.mcp_port = mcp_port or coordinated_mcp_port(coordination_file)
        self.profile_dir = None
        self.firefox_process = None

    @classmethod
    def _profile_fingerprint(cls):
        """Digest of everything that decides what a BUILT profile contains.

        Deliberately excludes the port: the port is stamped into the profile after it is
        restored (_apply_test_port), so a cached profile is reusable by any session on
        any port. Include anything here that would make a cached profile wrong — today
        that is the extension XPI's content and the optional permissions we pre-grant.
        """
        xpi = _get_extension_xpi_path()
        perms = ','.join(sorted(cls._GRANTED_OPTIONAL_PERMISSIONS))
        try:
            stat = os.stat(xpi) if xpi else None
            stamp = (xpi, stat.st_mtime_ns, stat.st_size, perms) if stat else (xpi, 0, 0, perms)
        except OSError:
            stamp = (xpi, 0, 0, perms)

        if stamp in cls._fingerprints:
            return cls._fingerprints[stamp]

        digest = hashlib.sha256()
        if xpi and os.path.exists(xpi):
            with open(xpi, 'rb') as f:
                for chunk in iter(lambda: f.read(1 << 20), b''):
                    digest.update(chunk)
        else:
            digest.update(b'no-xpi')
        digest.update(b'|perms=' + perms.encode())

        fingerprint = digest.hexdigest()[:12]
        cls._fingerprints[stamp] = fingerprint
        return fingerprint

    @property
    def cache_key(self):
        """Profile-cache slot for this manager — content-addressed, port-independent."""
        fingerprint = self._profile_fingerprint()
        if self.profile_tag:
            return f"{fingerprint}-{self.profile_tag}"
        return fingerprint

    @classmethod
    def _get_cache_dir(cls):
        """Get or create cache directory under dist/"""
        if cls._cache_dir is None:
            # Create cache directory under dist/
            dist_dir = project_root / 'dist'
            dist_dir.mkdir(exist_ok=True)

            cls._cache_dir = str(dist_dir / 'profile-cache')
            os.makedirs(cls._cache_dir, exist_ok=True)

            # Register cleanup only on first creation
            atexit.register(cls._cleanup_cache_dir)

        return cls._cache_dir

    @classmethod
    def _discover_cached_profiles(cls):
        """Discover existing cached profiles from previous runs"""
        cache_dir = cls._get_cache_dir()

        for filename in os.listdir(cache_dir):
            if filename.startswith('profile-') and filename.endswith('.tar.gz'):
                try:
                    # Extract the cache key from the filename:
                    #   profile-<fingerprint>.tar.gz     -> "<fingerprint>"      (default)
                    #   profile-<fingerprint>-b.tar.gz   -> "<fingerprint>-b"    (tagged)
                    # Port-keyed files from before the cache was re-keyed (profile-40400
                    # .tar.gz) simply never match a fingerprint, so they are never reused.
                    cache_key = filename[8:-7]  # Remove 'profile-' and '.tar.gz'

                    compressed_path = os.path.join(cache_dir, filename)
                    file_stat = os.stat(compressed_path)

                    # Add to cache if not already present
                    if cache_key not in cls._profile_cache:
                        entry = ProfileCacheEntry(
                            key=cache_key,
                            compressed_path=compressed_path,
                            created_at=file_stat.st_ctime,
                            last_used=file_stat.st_mtime,
                            use_count=0,  # Reset count for new session
                            is_locked=False
                        )
                        cls._profile_cache[cache_key] = entry

                except (ValueError, OSError):
                    # Skip invalid files
                    continue

    @classmethod
    def _cleanup_cache_dir(cls):
        """Clean up cache entries on exit (preserve directory structure)"""
        # Clear cache entries in memory
        cls._profile_cache.clear()
        print("✓ Profile cache entries cleared")

        # Note: We preserve the dist/profile-cache directory and files
        # for persistence between test runs

    def _get_cached_profile(self):
        """Retrieve cached profile if available and valid, stamped with THIS session's port."""
        # Discover cached profiles from previous runs on first access
        if not hasattr(self.__class__, '_discovery_done'):
            self._discover_cached_profiles()
            self.__class__._discovery_done = True

        cache_key = self.cache_key
        if cache_key not in self._profile_cache:
            return None

        entry = self._profile_cache[cache_key]

        # Every failure below FORGETS the entry and leaves the tarball alone: other
        # processes share it, and the locked rebuild in _create_test_profile replaces it
        # whole. A failure on this process's private copy (a scanner holding the sqlite
        # it just extracted) says nothing about the tarball.
        if not os.path.exists(entry.compressed_path):
            self._forget_cached_profile(cache_key)
            return None

        # Check if profile is locked (in use by another test)
        if entry.is_locked:
            return None

        try:
            # Extract compressed profile to temporary directory
            profile_dir = tempfile.mkdtemp(prefix='browserplug-extracted-')
            self._extract_profile(entry.compressed_path, profile_dir)

            # Validate extracted profile structure
            if not self._validate_cached_profile(profile_dir):
                shutil.rmtree(profile_dir, ignore_errors=True)
                self._forget_cached_profile(cache_key)
                return None

            # The cached profile was built for whatever port that session had. Point its
            # extension at OUR port before Firefox ever sees it — without this, a cached
            # profile would silently drive a stale (or another session's) server.
            if not self._apply_test_port(profile_dir):
                shutil.rmtree(profile_dir, ignore_errors=True)
                self._forget_cached_profile(cache_key)
                return None

            # Lock the profile for use and update stats
            entry.is_locked = True
            entry.last_used = time.time()
            entry.use_count += 1
            # The mtime is the cross-process "last used" that pruning reads.
            try:
                os.utime(entry.compressed_path)
            except OSError:
                pass
            return profile_dir

        except Exception as e:
            print(f"⚠ Error extracting cached profile: {e}")
            self._forget_cached_profile(cache_key)
            return None

    def _apply_test_port(self, profile_dir):
        """Stamp this session's test port into an already-built profile.

        The port lives in exactly two places a profile carries it:
          * user.js  — extensions.browserplug.testPort (also re-asserts the lifted network block)
          * storage-sync-v2.sqlite — the config browser.storage.sync actually serves to
            the extension's loadConfig()
        Both are rewritten, then read back: a profile that would still point the extension
        anywhere else (in particular at the developer's live server on
        ws://localhost:8772) is rejected rather than started.
        """
        old_profile_dir = self.profile_dir
        self.profile_dir = profile_dir
        try:
            self._write_user_js(bootstrap=False)

            db_path = self._storage_sync_db_path()
            if not os.path.exists(db_path):
                print("⚠ Cached profile has no storage-sync-v2.sqlite to re-seed")
                return False
            if not self._write_storage_sync_config(db_path):
                return False

            if not self._profile_points_at_test_port():
                print(f"⚠ Cached profile did not take test port {self.test_port}")
                return False

            print(f"✓ Cached profile re-seeded with test port {self.test_port}")
            return True
        except Exception as e:
            print(f"⚠ Error applying test port to cached profile: {e}")
            return False
        finally:
            self.profile_dir = old_profile_dir

    def _extract_profile(self, compressed_path, target_dir):
        """Extract compressed profile to target directory"""
        with tarfile.open(compressed_path, 'r:gz') as tar:
            tar.extractall(target_dir)

    def _compress_profile(self, profile_dir, output_path):
        """Compress profile directory to .tar.gz file"""
        with tarfile.open(output_path, 'w:gz') as tar:
            # Add all files in the profile directory
            for item in os.listdir(profile_dir):
                item_path = os.path.join(profile_dir, item)
                tar.add(item_path, arcname=item)

    def _validate_cached_profile(self, profile_path):
        """Validate that a cached profile is still usable.

        Port-INDEPENDENT on purpose: the cache is keyed on the profile's contents and the
        session's port is stamped in afterwards (_apply_test_port). What is checked here is
        everything a restored profile must already carry and that we cannot fix up:
        the extension, its enablement, the granted optional permissions, and the absence of
        the bootstrap network block.
        """
        try:
            # user.js must exist and carry a testPort override (any port — ours gets
            # stamped in next). A profile cached mid-bootstrap would have no working
            # network at all (the block is meant to be lifted before caching) - rebuild it.
            user_js_path = os.path.join(profile_path, 'user.js')
            if not os.path.exists(user_js_path):
                return False  # user.js should exist
            with open(user_js_path, 'r') as f:
                content = f.read()
            if 'extensions.browserplug.testPort' not in content:
                return False
            if 'user_pref("network.proxy.type", 1)' in content:
                print("⚠ Cached profile still carries the bootstrap network block")
                return False

            # Check extension is installed. Its CONTENT is already guaranteed current by
            # the cache key (the XPI's hash is part of the fingerprint), so a rebuilt-but-
            # identical XPI no longer needlessly invalidates the cache.
            extensions_dir = os.path.join(profile_path, 'extensions')
            extension_file = os.path.join(extensions_dir, 'browserplug@marcinwolny.net.xpi')
            if not os.path.exists(extension_file):
                return False  # Extension should be installed

            # Check extensions.json exists and has the extension enabled
            extensions_json_path = os.path.join(profile_path, 'extensions.json')
            if not os.path.exists(extensions_json_path):
                return False  # extensions.json should exist

            # A profile cached before an optional permission was added lacks the grant
            if not self._cached_profile_has_optional_permissions(profile_path):
                print("⚠ Cached profile is missing granted optional permissions")
                return False

            # The extension's config store must be there to re-seed; a profile without it
            # is one whose extension would fall back to the live server's default port.
            storage_db_path = os.path.join(profile_path, 'storage-sync-v2.sqlite')
            if not os.path.exists(storage_db_path):
                print("⚠ Cached profile has no seeded extension storage")
                return False

            return True
        except Exception:
            return False

    def _cache_profile(self, profile_path):
        """Compress a freshly built profile into the cache. The caller holds
        _profile_cache_lock and prunes afterwards (_prune_profile_cache)."""
        try:
            # Compress into a private staging file and swap it in whole, so a process
            # restoring this key never reads a half-written tarball.
            cache_dir = self._get_cache_dir()
            cache_key = self.cache_key
            compressed_path = os.path.join(cache_dir, f'profile-{cache_key}.tar.gz')
            staging_path = f'{compressed_path}.{os.getpid()}.tmp'
            self._compress_profile(profile_path, staging_path)
            # Windows refuses to replace a file another process is extracting, which
            # takes about a second, so retry for a few. If it still refuses, keep theirs:
            # the same key means the same contents, unless theirs failed validation, and
            # then the next miss rebuilds it.
            for attempt in range(10):
                try:
                    os.replace(staging_path, compressed_path)
                    break
                except OSError:
                    if attempt == 9:
                        os.remove(staging_path)
                    else:
                        time.sleep(0.5)

            # Add to cache
            entry = ProfileCacheEntry(
                key=cache_key,
                compressed_path=compressed_path,
                created_at=time.time(),
                last_used=time.time(),
                use_count=1,
                is_locked=True
            )
            self._profile_cache[cache_key] = entry

        except Exception as e:
            print(f"⚠ Error compressing profile for cache: {e}")
            # Don't cache if compression fails

    @classmethod
    def _prune_profile_cache(cls):
        """Delete the oldest tarballs beyond _max_cache_size. The caller holds
        _profile_cache_lock.

        Never deletes what another process may be restoring: the current XPI's entries
        (every process on this checkout uses those) and any tarball restored within
        _EVICTION_GRACE_SECONDS. So the cache can sit above its cap for a while after
        several rebuilds of the XPI in one afternoon. Also removes staging files that a
        dead build left behind.
        """
        cache_dir = cls._get_cache_dir()
        current = cls._profile_fingerprint()
        now = time.time()
        tarballs, candidates = [], []
        for name in os.listdir(cache_dir):
            path = os.path.join(cache_dir, name)
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if name.endswith('.tmp'):
                if now - mtime > cls._STALE_STAGING_SECONDS:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                continue
            if not (name.startswith('profile-') and name.endswith('.tar.gz')):
                continue
            key = name[8:-7]
            tarballs.append(key)
            if key == current or key.startswith(current + '-'):
                continue
            if now - mtime < cls._EVICTION_GRACE_SECONDS:
                continue
            candidates.append((mtime, key, path))

        excess = len(tarballs) - cls._max_cache_size
        for _, key, path in sorted(candidates)[:max(excess, 0)]:
            try:
                os.remove(path)
            except OSError as e:
                print(f"⚠ Could not evict cached profile {key}: {e}")
                continue
            cls._profile_cache.pop(key, None)
            print(f"✓ Evicted cached profile {key}")

    def _forget_cached_profile(self, cache_key):
        """Drop an entry from this process's view of the cache; the tarball stays."""
        self._profile_cache.pop(cache_key, None)

    def _create_test_profile(self):
        """Create or retrieve cached Firefox profile configured for testing"""

        # Settle the test port FIRST (a coordination file can override it): both the
        # cached and the freshly-built profile have to carry the final value.
        self._setup_extension_test_config()

        # Check cache first
        cached_profile = self._get_cached_profile()
        if cached_profile:
            self.profile_dir = cached_profile
            print(f"✓ Using cached profile {self.cache_key}: {self.profile_dir}")
            return self.profile_dir

        # A miss builds under the cache lock. Parallel workers start their first tests
        # together, so on a cold cache one builds while the rest wait here and then
        # restore what it stored.
        with _profile_cache_lock(self._get_cache_dir()):
            self._discover_cached_profiles()
            cached_profile = self._get_cached_profile()
            if cached_profile:
                self.profile_dir = cached_profile
                print(f"✓ Using cached profile {self.cache_key} (stored while waiting for "
                      f"the cache lock): {self.profile_dir}")
                return self.profile_dir

            self.profile_dir = self._create_new_profile()
            self._cache_profile(self.profile_dir)
            self._prune_profile_cache()
        print(f"✓ Created and cached new profile {self.cache_key}: {self.profile_dir}")
        return self.profile_dir

    def _create_new_profile(self):
        """Create a new profile (will be compressed for caching)"""
        profile_dir = tempfile.mkdtemp(prefix='browserplug-new-profile-')

        # Temporarily set profile_dir for extension config and installation
        old_profile_dir = self.profile_dir
        self.profile_dir = profile_dir
        try:
            # Guard 1+2 (see _BOOTSTRAP_NETWORK_BLOCK): every Firefox start from here to
            # the end of _install_extension is network-blocked, and the extension already
            # has its test-port config, so it can never reach the live server on :8772.
            self._write_user_js(bootstrap=True)
            if not self._seed_extension_storage():
                raise RuntimeError("Failed to seed extension storage for the test profile")

            # Install extension as part of profile creation
            extension_path = _get_extension_xpi_path()
            if not extension_path or not os.path.exists(extension_path):
                raise FileNotFoundError("Extension XPI not found. Run 'make package' first.")

            success = self._install_extension(extension_path)
            if not success:
                raise RuntimeError("Extension installation failed during profile creation")

            self._grant_optional_permissions()

            # Bootstrap over: lift the network block so the actual tests can use the
            # network (fixtures server, example.org, ...). This is also the user.js that
            # gets cached, so cached profiles start unblocked.
            self._write_user_js(bootstrap=False)

        finally:
            self.profile_dir = old_profile_dir

        return profile_dir

    def _write_user_js(self, bootstrap):
        """Write <profile>/user.js.

        bootstrap=True  -> adds the network block used while the profile is being built.
        bootstrap=False -> lifts it again for the real test run.
        """
        user_js_content = '''
// Extension installation settings
user_pref("xpinstall.signatures.required", false);
user_pref("extensions.autoDisableScopes", 0);
user_pref("extensions.enabledScopes", 15);
user_pref("dom.disable_open_during_load", false);
user_pref("browser.tabs.remote.autostart", false);


// Disable various Firefox features for faster testing
user_pref("browser.startup.homepage", "about:blank");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url.additional", "");
user_pref("browser.newtabpage.enabled", false);
user_pref("browser.newtab.url", "about:blank");

// Disable Firefox updates and telemetry for testing
user_pref("app.update.enabled", false);
user_pref("app.update.auto", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("datareporting.healthreport.uploadEnabled", false);

// Speed up for testing
user_pref("dom.max_script_run_time", 0);
user_pref("dom.max_chrome_script_run_time", 0);

// Keep the profile off Firefox Sync (the storage.sync data stays local to the profile,
// in storage-sync-v2.sqlite, which is what the extension reads and what we seed).
user_pref("services.sync.engine.extension-storage", false);
user_pref("identity.fxaccounts.enabled", false);

// FORCE override default WebSocket port to prevent connecting to development server
user_pref("extensions.browserplug.forceTestPort", true);
user_pref("extensions.browserplug.testPort", ''' + str(self.test_port) + ''');
'''
        user_js_content += (self._BOOTSTRAP_NETWORK_BLOCK if bootstrap
                            else self._BOOTSTRAP_NETWORK_BLOCK_LIFTED)

        with open(os.path.join(self.profile_dir, 'user.js'), 'w') as f:
            f.write(user_js_content)
        return True

    def _grant_optional_permissions(self):
        """Pre-grant the extension's optional permissions in the test profile.

        Without this, `proxy` is revoked in a fresh profile: the extension refuses to
        route (PROXY_PERMISSION_REQUIRED) and the tools don't even expose the `proxy`
        arg, so the proxy-routing tests skip.
        """
        prefs_path = os.path.join(self.profile_dir, 'extension-preferences.json')
        try:
            with open(prefs_path, 'r') as f:
                prefs = json.load(f)
        except (OSError, ValueError):
            prefs = {}  # Firefox creates it on demand; a fresh profile has none

        entry = prefs.setdefault(self._EXTENSION_ID, {})
        granted = entry.get('permissions') or []
        for perm in self._GRANTED_OPTIONAL_PERMISSIONS:
            if perm not in granted:
                granted.append(perm)
        entry['permissions'] = granted
        entry.setdefault('origins', [])
        entry.setdefault('data_collection', [])

        with open(prefs_path, 'w') as f:
            json.dump(prefs, f)

        print(f"✓ Pre-granted optional permissions {self._GRANTED_OPTIONAL_PERMISSIONS} "
              f"for {self._EXTENSION_ID}")
        return True

    def _cached_profile_has_optional_permissions(self, profile_path):
        """True when the cached profile already grants every optional permission we need.

        Profiles cached before a permission was added to the list lack it, so they must
        be rebuilt rather than reused (otherwise the tests silently skip again).
        """
        try:
            with open(os.path.join(profile_path, 'extension-preferences.json'), 'r') as f:
                granted = json.load(f).get(self._EXTENSION_ID, {}).get('permissions', [])
        except (OSError, ValueError):
            return False
        return all(p in granted for p in self._GRANTED_OPTIONAL_PERMISSIONS)

    def _setup_extension_test_config(self):
        """Settle which port the extension must use (coordination file wins).

        This only resolves self.test_port — it must therefore work BEFORE a profile
        directory exists (the cached-profile path needs the final port too). The port
        reaches the extension via _seed_extension_storage() / _apply_test_port() ->
        storage-sync-v2.sqlite; the coordination path's own config.json
        (browser-extension-data/) is NOT read by Firefox at all.
        """
        # Use coordination file if available, otherwise use test_port
        if self.coordination_file:
            try:
                self.test_port = FirefoxPortCoordinator.read_ports(
                    self.coordination_file
                )['websocket']
                print(f"✓ Extension configured from coordination file for port {self.test_port}")
                return
            except Exception as e:
                print(f"⚠ Failed to use coordination file: {e}, falling back to direct config")

        print(f"✓ Pre-configured extension for test server port {self.test_port}")

    def _storage_sync_db_path(self):
        """Path of the ONLY file the extension's connection config can come from."""
        return os.path.join(self.profile_dir, 'storage-sync-v2.sqlite')

    def _test_storage_config(self):
        """The storage.sync payload the extension's loadConfig() expects."""
        config = {
            'hostname': 'localhost',
            'port': self.test_port,          # use dynamic test port
            'retryInterval': 1000,
            'maxRetries': 5,
            'pingTimeout': 2000,
            # Test configuration overrides (extension will use these)
            'testPort': self.test_port,
            'testHostname': 'localhost'
        }
        if self.mcp_port:
            # The extension's token bootstrap fetches /ws-token only from the MCP port it
            # is configured for (or 2830..2942); the test server's is random.
            config['mcpPort'] = self.mcp_port
        return config

    def _seed_extension_storage(self):
        """Point the extension at the test port BEFORE Firefox ever runs (guard 1).

        This replaces a former `_preconfigure_extension_storage()` that wrote
        browser-extension-data/<ext-id>/storage.json — a file Firefox never reads. The
        extension's loadConfig() reads browser.storage.sync, whose backing store is
        storage-sync-v2.sqlite, so an unseeded profile fell back to the default
        ws://localhost:8772 during bootstrap and hijacked the developer's live server.

        Hand-creating the DB is safe: Firefox's webext-storage opens an existing database
        whose user_version matches its own schema rather than recreating it (live-verified
        on FF153: the seeded row survives startup and the extension connects to it).
        """
        db_path = self._storage_sync_db_path()
        try:
            if not os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                try:
                    conn.executescript(self._STORAGE_SYNC_SCHEMA)
                    conn.commit()
                finally:
                    conn.close()
                print("✓ Created storage-sync-v2.sqlite for the test profile")

            if not self._write_storage_sync_config(db_path):
                return False

            print(f"✓ Seeded extension storage with test port {self.test_port} "
                  f"(before Firefox's first start)")
            return True

        except Exception as e:
            print(f"✗ Failed to seed extension storage: {e}")
            return False

    def _write_storage_sync_config(self, db_path):
        """Upsert the test config into storage-sync-v2.sqlite, tolerating a WAL lock.

        A Firefox we just terminated can still hold the WAL lock for a moment
        (TerminateProcess is a hard kill; straggler content processes release handles
        asynchronously), which surfaces as sqlite "disk I/O error" / "database is locked".
        Retry briefly instead of failing the whole profile setup.
        """
        config_json = json.dumps(self._test_storage_config())

        last_err = None
        for attempt in range(5):
            if attempt:
                time.sleep(1.0)
            try:
                conn = sqlite3.connect(db_path, timeout=5.0)
                try:
                    conn.execute(
                        "INSERT INTO storage_sync_data (ext_id, data, sync_change_counter)"
                        " VALUES (?, ?, 1)"
                        " ON CONFLICT(ext_id) DO UPDATE SET"
                        "   data = excluded.data,"
                        "   sync_change_counter = sync_change_counter + 1",
                        (self._EXTENSION_ID, config_json)
                    )
                    conn.commit()
                    return True
                finally:
                    conn.close()
            except sqlite3.Error as e:
                last_err = e

        print(f"✗ Failed to write extension storage after 5 attempts: {last_err}")
        return False

    def _profile_points_at_test_port(self):
        """True when the profile's storage.sync really serves THIS session's test port.

        The one thing that keeps a test profile off the developer's live server on
        ws://localhost:{DEFAULT_EXTENSION_WS_PORT}: the extension's loadConfig() reads
        this store, and if it holds nothing (or the wrong thing) the extension falls back
        to that default. Checked on the seeded bootstrap profile AND on every restored
        cached profile.
        """
        try:
            conn = sqlite3.connect(self._storage_sync_db_path(), timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT data FROM storage_sync_data WHERE ext_id = ?",
                    (self._EXTENSION_ID,)
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return False

        if not row:
            return False
        config = json.loads(row[0])
        seeded_port = config.get('testPort') or config.get('port')
        return (seeded_port == self.test_port
                and seeded_port != DEFAULT_EXTENSION_WS_PORT)

    def _bootstrap_isolation_active(self):
        """True when BOTH guards are in place for the profile about to run Firefox.

        Checked (not assumed) right before every bootstrap Firefox start: this is the
        invariant that keeps a test profile off the developer's live server on
        ws://localhost:{DEFAULT_EXTENSION_WS_PORT}. Fail closed — no guards, no Firefox.
        """
        try:
            with open(os.path.join(self.profile_dir, 'user.js'), 'r') as f:
                user_js = f.read()
        except OSError:
            return False

        blocked = all(marker in user_js for marker in (
            'user_pref("network.proxy.type", 1)',
            'user_pref("network.proxy.socks_port", 1)',
            'user_pref("network.proxy.allow_hijacking_localhost", true)',
        ))
        if not blocked:
            return False

        # ...and the extension already knows the test port, so it has no reason to try
        # the default one in the first place.
        return self._profile_points_at_test_port()

    def _install_extension(self, extension_path):
        """Install extension to the test profile and initialize extensions.json"""
        if not self.profile_dir:
            raise ValueError("Test profile not created. Call _create_test_profile() first.")

        extensions_dir = os.path.join(self.profile_dir, 'extensions')
        os.makedirs(extensions_dir, exist_ok=True)

        # Copy extension XPI to extensions directory
        if not os.path.exists(extension_path):
            print(f"✗ Extension not found: {extension_path}")
            return False

        extension_name = 'browserplug@marcinwolny.net.xpi'
        dest_path = os.path.join(extensions_dir, extension_name)
        shutil.copy2(extension_path, dest_path)
        print(f"✓ Extension copied to test profile: {extension_name}")

        # The Firefox runs below start with the extension INSTALLED AND ENABLED. They are
        # only safe because the caller (_create_new_profile) has already seeded the
        # extension's storage AND written the bootstrap network block into user.js —
        # without both, the extension defaults to ws://localhost:8772 and takes over the
        # developer's live server. Never call this without that setup.
        if not self._bootstrap_isolation_active():
            print("✗ Refusing to run Firefox: bootstrap isolation is not in place")
            return False

        # Run Firefox temporarily to initialize extensions.json
        if not self._initialize_extensions_json():
            return False

        # Enable the extension in extensions.json
        if not self._enable_extension_in_profile():
            return False

        # Ensure extension storage is properly configured (may be redundant but safe)
        if not self._configure_extension_storage():
            return False

        print("✓ Extension installed and enabled in test profile")
        return True

    def _initialize_extensions_json(self):
        """Run Firefox temporarily to create extensions.json"""
        firefox_path = os.path.expanduser(self.firefox_path)

        # Start Firefox temporarily with timeout
        firefox_cmd = [
            firefox_path,
            '-profile', self.profile_dir,
            '-no-remote',
            '-headless'
        ]

        print("⏳ Running Firefox temporarily to initialize extensions.json...")

        proc = None
        try:
            # Start Firefox process with timeout
            proc = subprocess.Popen(
                firefox_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # Wait for extensions.json to be created (up to 20 seconds)
            extensions_json_path = os.path.join(self.profile_dir, 'extensions.json')
            timeout = 20
            waited = 0

            while not os.path.exists(extensions_json_path) and waited < timeout:
                time.sleep(1)
                waited += 1

            # Give it one more second after creation
            if os.path.exists(extensions_json_path):
                time.sleep(1)
                print("✓ extensions.json created")
            else:
                print(f"✗ extensions.json not created after {timeout} seconds")
                _stop_firefox_tree(proc, self.profile_dir, _exe_name(firefox_path))
                return False

            # Every process of this run must be gone before the profile is cached: a
            # survivor keeps parent.lock, so caching fails and the test's own Firefox
            # cannot open the profile.
            _stop_firefox_tree(proc, self.profile_dir, _exe_name(firefox_path))
            print("✓ Firefox initialization process stopped")
            return True

        except Exception as e:
            print(f"✗ Failed to initialize extensions.json: {e}")
            if proc is not None:
                _stop_firefox_tree(proc, self.profile_dir, _exe_name(firefox_path))
            return False

    def _enable_extension_in_profile(self):
        """Enable the extension by modifying extensions.json"""
        extensions_json_path = os.path.join(self.profile_dir, 'extensions.json')

        if not os.path.exists(extensions_json_path):
            print("✗ extensions.json not found")
            return False

        try:
            # Pure-Python edit — no external `jq`/`which` (fragile on Windows, where
            # neither is on the subprocess PATH, so a cold profile-cache build fails).
            # extensions.json is UTF-8 (localized add-on descriptions).
            with open(extensions_json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            addons = data.get('addons') or []
            enabled = False
            for addon in addons:
                if addon.get('id') == 'browserplug@marcinwolny.net':
                    addon['userDisabled'] = False
                    addon['active'] = True
                    enabled = True

            if not enabled:
                # Firefox ran but never registered the XPI — a real install failure.
                print("✗ browserplug@marcinwolny.net not present in extensions.json — cannot enable")
                return False

            with open(extensions_json_path, 'w', encoding='utf-8') as f:
                json.dump(data, f)

            print("✓ Extension enabled in extensions.json")
            return True

        except Exception as e:
            print(f"✗ Failed to enable extension: {e}")
            return False

    def _configure_extension_storage(self):
        """Re-assert the extension's storage config after the bootstrap Firefox runs.

        The config is already seeded (_seed_extension_storage) before Firefox's first
        start; this rewrites it in case Firefox rewrote/migrated the database while it
        was running. It must stay INSIDE the bootstrap phase (network still blocked).
        """
        try:
            storage_db_path = self._storage_sync_db_path()

            if not os.path.exists(storage_db_path):
                # Only reachable if Firefox deleted the seeded DB (it doesn't, on FF153).
                print("✗ Storage database missing after bootstrap - profile is unusable")
                return False

            if not self._write_storage_sync_config(storage_db_path):
                return False

            print(f"✓ Extension storage configured with test port {self.test_port}")
            return True

        except Exception as e:
            print(f"✗ Failed to configure extension storage: {e}")
            return False

    def _start_firefox(self, headless=True, wait_for_startup=True):
        """Start Firefox with the test profile and extension"""
        if not self.profile_dir:
            raise ValueError("Test profile not created. Call _create_test_profile() first.")

        # Expand Firefox path
        firefox_path = os.path.expanduser(self.firefox_path)

        # Firefox command
        firefox_cmd = [
            firefox_path,
            '-profile', self.profile_dir,
            '-no-remote',
        ]

        if headless:
            firefox_cmd.append('-headless')

        try:
            self.firefox_process = subprocess.Popen(
                firefox_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            if wait_for_startup:
                time.sleep(FIREFOX_TEST_CONFIG['firefox_startup_wait'])

                # Check if process is still running
                if self.firefox_process.poll() is not None:
                    raise Exception(f"Firefox process exited immediately (code: {self.firefox_process.returncode})")

            print(f"✓ Firefox started with test profile (PID: {self.firefox_process.pid})")
            return True

        except Exception as e:
            print(f"✗ Failed to start Firefox: {e}")
            return False

    def wait_for_extension_connection(self, timeout=10.0, server=None):
        """Wait for extension to connect to test server

        Args:
            timeout: Maximum time to wait for connection
            server: Optional BrowserPlugServer instance to use for awaitable connection check

        Returns:
            bool: True if connection was established, False otherwise
        """
        print(f"Waiting up to {timeout}s for extension to connect to port {self.test_port}...")

        if server and hasattr(server, 'wait_for_extension_connection'):
            # Use server's awaitable connection mechanism if available
            import asyncio
            try:
                # Create new event loop if none exists
                loop = None
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                # Run the async wait_for_extension_connection method
                connected = loop.run_until_complete(
                    server.wait_for_extension_connection(timeout=timeout)
                )

                if connected:
                    print("✓ Extension connected to server")
                    return True
                else:
                    print("✗ Extension connection timeout")
                    return False

            except Exception as e:
                print(f"✗ Error waiting for extension connection: {e}")
                # Fallback to time-based waiting
                pass

        # Fallback: use time-based waiting
        time.sleep(FIREFOX_TEST_CONFIG['extension_install_wait'])

        if not (self.firefox_process and self.firefox_process.poll() is None):
            print("✗ Firefox process not running")
            return False
        # A live PROCESS is not a live CONNECTION. The old code returned True here and
        # reported "connected" for a Firefox whose extension never opened a socket —
        # exactly the masking this suite forbids. Verify against the server registry when
        # we have one; without a server we cannot confirm, so do NOT claim success.
        if server is not None and hasattr(server, 'live_browsers'):
            if server.live_browsers():
                print("✓ Extension connected (server registry shows a live browser)")
                return True
            print("✗ Firefox is running but no browser registered with the server")
            return False
        print("⚠ Firefox running but no server to verify the connection — not claiming success")
        return False

    async def async_wait_for_extension_connection(self, timeout=10.0, server=None):
        """Async version of wait_for_extension_connection for use in async test contexts

        Args:
            timeout: Maximum time to wait for connection
            server: Optional BrowserPlugServer instance to use for awaitable connection check

        Returns:
            bool: True if connection was established, False otherwise
        """
        print(f"Waiting up to {timeout}s for extension to connect to port {self.test_port}...")

        if server and hasattr(server, 'wait_for_extension_connection'):
            # Use server's awaitable connection mechanism directly
            try:
                connected = await server.wait_for_extension_connection(timeout=timeout)

                if connected:
                    print("✓ Extension connected to server")
                    return True
                else:
                    print("✗ Extension connection timeout")
                    return False

            except Exception as e:
                print(f"✗ Error waiting for extension connection: {e}")
                # Fallback to time-based waiting
                pass

        # Fallback: use time-based waiting with async sleep
        import asyncio
        await asyncio.sleep(FIREFOX_TEST_CONFIG['extension_install_wait'])

        if not (self.firefox_process and self.firefox_process.poll() is None):
            print("✗ Firefox process not running")
            return False
        # A live PROCESS is not a live CONNECTION (see the sync twin above): only claim
        # success when the server registry actually shows a browser, never on liveness.
        if server is not None and hasattr(server, 'live_browsers'):
            if server.live_browsers():
                print("✓ Extension connected (server registry shows a live browser)")
                return True
            print("✗ Firefox is running but no browser registered with the server")
            return False
        print("⚠ Firefox running but no server to verify the connection — not claiming success")
        return False

    def stop_firefox(self):
        """Stop Firefox, its child processes, and anything else still running on the
        profile (_stop_firefox_tree says why each is needed)."""
        if self.firefox_process:
            try:
                forced = _stop_firefox_tree(self.firefox_process, self.profile_dir,
                                            _exe_name(self.firefox_path))
                print("⚠ Firefox force killed" if forced else "✓ Firefox stopped")
            except Exception as e:
                print(f"⚠ Error stopping Firefox: {e}")
            finally:
                self.firefox_process = None

    def cleanup(self):
        """Clean up test profile and stop Firefox - modified for compressed caching"""
        self.stop_firefox()

        # Always delete the extracted profile directory
        if self.profile_dir and os.path.exists(self.profile_dir):
            _remove_profile_dir_in_background(self.profile_dir)
            print("✓ Extracted profile removal started")

        # Unlock the cached compressed profile if it exists
        cache_key = self.cache_key
        if cache_key in self._profile_cache:
            entry = self._profile_cache[cache_key]
            entry.is_locked = False
            entry.last_used = time.time()
            print(f"✓ Cached profile {cache_key} unlocked (available for reuse)")

        self.profile_dir = None

    def setup_and_start_firefox(self, headless=True, wait_for_startup=True, skip_on_failure=True):
        """Combine create_test_profile, install_extension, and start_firefox.

        Returns False ONLY for the one legitimate skip reason — the Firefox binary is not
        on this box. Every OTHER failure (missing XPI, broken profile build, crashed
        bootstrap) RAISES with diagnostics: those are real breakage and must not be masked
        behind a False that conftest turns into a green pytest.skip.

        Args:
            headless: Whether to run Firefox in headless mode (default: True)
            wait_for_startup: Whether to wait for Firefox startup (default: True)
            skip_on_failure: governs ONLY the binary-not-found case — True returns False
                (caller may skip), False raises. Genuine setup failures raise regardless.

        Returns:
            bool: True if all steps succeeded; False only if the Firefox binary is missing
                and skip_on_failure=True.

        Raises:
            RuntimeError: on any genuine setup failure (binary present but setup broke),
                or on a missing binary when skip_on_failure=False.
        """
        firefox_path = os.path.expanduser(self.firefox_path)
        if not os.path.exists(firefox_path):
            # The ONE legitimate skip: no browser installed on this box.
            print(f"✗ Firefox binary not found at {firefox_path}")
            if skip_on_failure:
                return False
            raise RuntimeError(f"Firefox binary not found at {firefox_path}")

        # Binary exists -> any failure below is real. Surface it loudly (with the original
        # traceback) instead of collapsing it to a False. skip_on_failure no longer hides
        # these: a broken profile/XPI is never a reason to skip the suite.
        try:
            # Step 1: Create test profile (includes extension installation)
            self._create_test_profile()

            # Step 2: Start Firefox
            firefox_started = self._start_firefox(headless=headless, wait_for_startup=wait_for_startup)
            if not firefox_started:
                raise RuntimeError(
                    "Firefox failed to start (binary present, profile built, but the "
                    "process exited immediately — see the captured output above)")

            return True

        except Exception as e:
            raise RuntimeError(
                f"Firefox test setup failed after the binary was found at {firefox_path}: "
                f"{type(e).__name__}: {e} — this is a real profile/XPI/bootstrap failure, "
                "not a missing browser, so it is not skippable"
            ) from e

    def __enter__(self):
        """Context manager entry"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup"""
        self.cleanup()


def start_additional_firefox(test_port, profile_tag='b', firefox_path=None, headless=True,
                             mcp_port=None):
    """Start a SECOND (third, ...) Firefox against an already-running test server.

    Same XPI, same server, same WebSocket port — but its OWN profile directory (and its
    own profile-cache slot, via profile_tag), which is what makes it a DIFFERENT browser:
    the extension mints its instance_id per profile, so this Firefox becomes a second
    entry in the server's browser registry instead of displacing the first one's socket.

    It goes through exactly the same profile bootstrap as the first browser, so both
    isolation guards apply (seeded storage-sync-v2.sqlite + the bootstrap network block):
    this profile can never fall back to ws://localhost:8772 and hijack the developer's
    live Firefox session.

    Returns a started FirefoxTestManager, or None if setup/startup failed (the caller
    should pytest.skip). The caller owns cleanup() — always in a finally.
    """
    manager = FirefoxTestManager(
        firefox_path=firefox_path or os.environ.get('FIREFOX_PATH', 'firefox'),
        test_port=test_port,
        profile_tag=profile_tag,
        mcp_port=mcp_port,
    )
    try:
        started = manager.setup_and_start_firefox(headless=headless)
    except Exception:
        # A genuine setup/XPI/bootstrap failure now RAISES (no longer masked as a
        # False->skip). Don't leak the profile/Firefox — clean up, then re-raise loudly.
        manager.cleanup()
        raise
    if not started:
        # False now means only the legitimate "Firefox binary not found" skip.
        manager.cleanup()
        return None
    return manager


async def async_wait_for_browser_count(server, count, timeout=20.0, identified=True):
    """Wait until the server's browser registry holds exactly `count` live browsers.

    The server's own wait_for_extension_connection() only answers "is ANY browser
    connected", which cannot see a SECOND Firefox arriving (or a killed one leaving).
    This polls live_browsers() — the same view routing uses.

    identified=True (default) also requires every browser to have been NAMED by its
    extension_hello (provisional=False): a socket is registered the moment it connects,
    but its instance_id — its identity in the registry, and what a selection points at —
    only lands with the hello.

    Returns True on success, False on timeout.
    """
    import asyncio
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        browsers = server.live_browsers()
        if len(browsers) == count and (
                not identified or all(not b.provisional for b in browsers)):
            return True
        await asyncio.sleep(0.25)
    return False


def validate_history_item_timestamp(item, field_name="lastVisitTime"):
    """
    Validate that a history item has a valid timestamp.

    Ensures:
    1. Timestamp field is present (not None)
    2. Timestamp is > 0
    3. Timestamp is a valid number
    4. Timestamp represents a reasonable date (after year 2000, not in future)

    Args:
        item: History item dict to validate
        field_name: Name of timestamp field (default: "lastVisitTime")

    Raises:
        AssertionError: If timestamp validation fails with detailed message
    """
    from datetime import datetime

    # Check field exists and is not None
    assert field_name in item, f"History item missing '{field_name}' field. Item: {item}"

    timestamp = item[field_name]
    assert timestamp is not None, f"History item '{field_name}' is None. Item: {item}"

    # Check timestamp is a number
    assert isinstance(timestamp, (int, float)), \
        f"History item '{field_name}' must be a number, got {type(timestamp).__name__}: {timestamp}"

    # Check timestamp is positive
    assert timestamp > 0, \
        f"History item '{field_name}' must be > 0, got: {timestamp}"

    # Convert milliseconds to seconds for datetime validation
    timestamp_seconds = timestamp / 1000

    # Check timestamp represents a valid date (after year 2000)
    # Year 2000-01-01 00:00:00 UTC = 946684800 seconds
    year_2000_ms = 946684800 * 1000
    assert timestamp >= year_2000_ms, \
        f"History item '{field_name}' represents date before year 2000: {datetime.fromtimestamp(timestamp_seconds)}"

    # Check timestamp is not in the future (allow 60 second clock skew)
    now_ms = datetime.now().timestamp() * 1000
    max_future_ms = now_ms + 60000  # 60 seconds in future
    assert timestamp <= max_future_ms, \
        f"History item '{field_name}' is in the future: {datetime.fromtimestamp(timestamp_seconds)} (now: {datetime.now()})"

    return True


def _get_extension_xpi_path():
    """Get path to built extension XPI file"""
    xpi_path = project_root / 'dist' / 'firefox' / 'browserplug@marcinwolny.net.xpi'

    if xpi_path.exists():
        return str(xpi_path)

    # Second try: search upward for the dist directory
    search_path = Path(__file__).resolve().parent
    while search_path != search_path.parent:  # Stop at filesystem root
        potential_xpi = search_path / 'dist' / 'firefox' / 'browserplug@marcinwolny.net.xpi'
        if potential_xpi.exists():
            return str(potential_xpi)
        search_path = search_path.parent

    # Third try: alternative location in extension directory
    alt_path = project_root / 'extension-gecko' / 'browserplug@marcinwolny.net.xpi'
    if alt_path.exists():
        return str(alt_path)

    return None
