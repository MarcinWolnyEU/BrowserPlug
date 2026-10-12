#!/usr/bin/env python3
"""
Regression tests: a TEST profile must never be able to reach the developer's LIVE
BrowserPlug server on ws://localhost:8772.

Building a profile runs Firefox a couple of times with the extension installed and
ENABLED (extensions.json bootstrap). If that Firefox's extension has no stored config it
falls back to its built-in default -- ws://localhost:8772 -- connects to the developer's
shared server, and takes over its single extension slot: MCP tool calls meant for the
real, logged-in browser get answered by a throwaway headless test profile.

The harness guards this twice (see FirefoxTestManager):
  1. the extension's connection config is seeded into storage-sync-v2.sqlite (the file
     browser.storage.sync actually reads) BEFORE Firefox's first start, and
  2. the bootstrap-phase user.js routes ALL traffic, loopback included, through a dead
     SOCKS proxy, so even an unconfigured extension cannot reach any server.

These tests fail if either guard regresses. No Firefox required: the Firefox launches
are stubbed, and the stubs assert on the profile as it would be handed to Firefox.
"""

import json
import os
import shutil
import sqlite3

import pytest

import firefox_test_utils
from firefox_test_utils import DEFAULT_EXTENSION_WS_PORT, FirefoxTestManager

TEST_PORT = 41999


def _read_storage_sync(profile_dir):
    """Read the extension's config exactly the way Firefox's storage.sync does."""
    db_path = os.path.join(profile_dir, 'storage-sync-v2.sqlite')
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT data FROM storage_sync_data WHERE ext_id = ?",
            ('browserplug@marcinwolny.net',)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """Build a profile with every Firefox launch stubbed, capturing bootstrap state.

    Returns (profile_dir, bootstrap_states) where bootstrap_states is the list of
    profile snapshots taken at the moment each bootstrap Firefox would have started.
    """
    bootstrap_states = []

    fake_xpi = tmp_path / 'browserplug@marcinwolny.net.xpi'
    fake_xpi.write_bytes(b'PK\x03\x04 not a real xpi')
    monkeypatch.setattr(firefox_test_utils, '_get_extension_xpi_path', lambda: str(fake_xpi))

    def fake_initialize_extensions_json(self):
        # This is where Firefox really starts, with the extension enabled. Snapshot what
        # it would see, then fake the artifact its run produces.
        with open(os.path.join(self.profile_dir, 'user.js')) as f:
            user_js = f.read()
        bootstrap_states.append({
            'user_js': user_js,
            'storage': _read_storage_sync(self.profile_dir),
        })
        with open(os.path.join(self.profile_dir, 'extensions.json'), 'w') as f:
            json.dump({'addons': [{'id': 'browserplug@marcinwolny.net', 'active': True}]}, f)
        return True

    monkeypatch.setattr(FirefoxTestManager, '_initialize_extensions_json',
                        fake_initialize_extensions_json)
    # Needs jq + a real extensions.json; irrelevant to isolation.
    monkeypatch.setattr(FirefoxTestManager, '_enable_extension_in_profile', lambda self: True)

    # Any OTHER attempt to launch Firefox during profile setup is a bug: it would run an
    # unguarded browser. (Popen is how every launch in the harness is spawned.)
    def forbidden_popen(*args, **kwargs):
        raise AssertionError(f"unexpected Firefox launch during profile setup: {args}")
    monkeypatch.setattr(firefox_test_utils.subprocess, 'Popen', forbidden_popen)

    mgr = FirefoxTestManager(firefox_path='firefox', test_port=TEST_PORT)
    profile_dir = mgr._create_new_profile()   # bypasses the cache on purpose
    try:
        yield profile_dir, bootstrap_states
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def test_bootstrap_firefox_runs_at_all(profile):
    """Guard the guards: the assertions below are meaningless if nothing bootstrapped."""
    _, bootstrap_states = profile
    assert bootstrap_states, "no bootstrap Firefox start was captured"


def test_extension_is_configured_before_firefox_ever_starts(profile):
    """Guard 1: storage.sync (storage-sync-v2.sqlite) already holds the TEST port."""
    _, bootstrap_states = profile

    for i, state in enumerate(bootstrap_states):
        config = state['storage']
        assert config is not None, (
            f"bootstrap start #{i}: extension storage is empty, so the extension would "
            f"fall back to ws://localhost:{DEFAULT_EXTENSION_WS_PORT} - the developer's "
            f"live server. The config must be seeded into storage-sync-v2.sqlite "
            f"(browser.storage.sync); a JSON file under browser-extension-data/ is NOT "
            f"read by Firefox."
        )
        assert config.get('testPort') == TEST_PORT, f"bootstrap start #{i}: {config}"
        assert config.get('port') == TEST_PORT, f"bootstrap start #{i}: {config}"
        assert DEFAULT_EXTENSION_WS_PORT not in (config.get('port'), config.get('testPort')), \
            f"bootstrap start #{i}: test profile points at the live server port"


def test_bootstrap_firefox_cannot_reach_the_network_at_all(profile):
    """Guard 2: the bootstrap user.js routes everything through a dead SOCKS proxy."""
    _, bootstrap_states = profile

    for i, state in enumerate(bootstrap_states):
        user_js = state['user_js']
        missing = [pref for pref in (
            'user_pref("network.proxy.type", 1)',
            'user_pref("network.proxy.socks", "127.0.0.1")',
            'user_pref("network.proxy.socks_port", 1)',
            # Without this, Firefox refuses to proxy loopback and sends
            # ws://localhost:8772 straight to the live server.
            'user_pref("network.proxy.allow_hijacking_localhost", true)',
            # Without this, a dead proxy can fall back to a direct connection.
            'user_pref("network.proxy.failover_direct", false)',
        ) if pref not in user_js]
        assert not missing, (
            f"bootstrap start #{i}: user.js is missing {missing} - a test profile's "
            f"extension could reach ws://localhost:{DEFAULT_EXTENSION_WS_PORT}"
        )


def test_finished_profile_has_the_network_block_lifted(profile):
    """The block is bootstrap-only: real test runs need the network (fixtures, example.org).

    user.js prefs are persisted into prefs.js, so the final user.js must explicitly
    reverse the block, not merely omit it.
    """
    profile_dir, _ = profile
    with open(os.path.join(profile_dir, 'user.js')) as f:
        user_js = f.read()

    assert 'user_pref("network.proxy.type", 0)' in user_js
    assert 'user_pref("network.proxy.allow_hijacking_localhost", false)' in user_js
    assert 'user_pref("network.proxy.type", 1)' not in user_js
    assert f'testPort", {TEST_PORT}' in user_js          # cache validation depends on this


def test_finished_profile_keeps_the_test_port_in_storage(profile):
    """The profile that gets cached still points the extension at the test port."""
    profile_dir, _ = profile
    config = _read_storage_sync(profile_dir)
    assert config['port'] == TEST_PORT and config['testPort'] == TEST_PORT


def test_install_refuses_to_launch_firefox_without_the_guards(tmp_path, monkeypatch):
    """Fail closed: no seeded storage / no network block => no bootstrap Firefox."""
    launched = []
    monkeypatch.setattr(firefox_test_utils.subprocess, 'Popen',
                        lambda *a, **k: launched.append(a))

    fake_xpi = tmp_path / 'ext.xpi'
    fake_xpi.write_bytes(b'PK\x03\x04')

    mgr = FirefoxTestManager(firefox_path='firefox', test_port=TEST_PORT)
    mgr.profile_dir = str(tmp_path / 'bare-profile')
    os.makedirs(mgr.profile_dir)
    # A profile with neither guard: exactly the state that used to hijack the live server.
    assert mgr._bootstrap_isolation_active() is False
    assert mgr._install_extension(str(fake_xpi)) is False
    assert not launched, "Firefox was launched despite the isolation guards being absent"


def _fabricate_cached_profile(tmp_path, mgr, user_js):
    """An otherwise-valid cached profile, so a rejection is attributable to `user_js`."""
    profile_dir = tmp_path / 'cached'
    (profile_dir / 'extensions').mkdir(parents=True)
    (profile_dir / 'extensions' / 'browserplug@marcinwolny.net.xpi').write_bytes(b'x')
    (profile_dir / 'extensions.json').write_text('{}')
    (profile_dir / 'extension-preferences.json').write_text(
        json.dumps({'browserplug@marcinwolny.net': {'permissions': ['proxy']}}))
    (profile_dir / 'user.js').write_text(user_js)

    old, mgr.profile_dir = mgr.profile_dir, str(profile_dir)
    try:
        assert mgr._seed_extension_storage()
    finally:
        mgr.profile_dir = old
    return profile_dir


def test_cached_profile_with_the_bootstrap_block_is_rejected(tmp_path):
    """A profile cached mid-bootstrap would have no network - it must not be reused."""
    mgr = FirefoxTestManager(firefox_path='firefox', test_port=TEST_PORT)
    profile_dir = _fabricate_cached_profile(
        tmp_path, mgr,
        f'user_pref("extensions.browserplug.testPort", {TEST_PORT});\n'
        'user_pref("network.proxy.type", 1);\n'
    )
    assert mgr._validate_cached_profile(str(profile_dir)) is False

    # ...and the block is the ONLY reason it was rejected (otherwise this test would
    # keep passing for an unrelated defect).
    (profile_dir / 'user.js').write_text(
        f'user_pref("extensions.browserplug.testPort", {TEST_PORT});\n'
        'user_pref("network.proxy.type", 0);\n'
    )
    assert mgr._validate_cached_profile(str(profile_dir)) is True


def test_cached_profile_without_seeded_storage_is_rejected(tmp_path):
    """No storage.sync store => the extension falls back to the live server's port."""
    mgr = FirefoxTestManager(firefox_path='firefox', test_port=TEST_PORT)
    profile_dir = _fabricate_cached_profile(
        tmp_path, mgr,
        f'user_pref("extensions.browserplug.testPort", {TEST_PORT});\n'
        'user_pref("network.proxy.type", 0);\n'
    )
    os.remove(profile_dir / 'storage-sync-v2.sqlite')
    assert mgr._validate_cached_profile(str(profile_dir)) is False
