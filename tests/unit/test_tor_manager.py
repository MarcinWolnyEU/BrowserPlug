"""server/tor/manager.py against a stand-in `tor` and a fake download: install, start,
retry, stop (.tmp/dev-docs/tor.md). The real bundle is exercised by tests/docker/tor_smoke.py."""

import errno
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile

import pytest

from server.tor import bundle, manager

FAKE_TOR = r'''
import json, os, sys, time
mode = os.environ.get("FAKE_TOR_MODE", "ok")
if os.environ.get("FAKE_TOR_ARGV"):
    with open(os.environ["FAKE_TOR_ARGV"], "w") as fh:
        json.dump({"argv": sys.argv[1:], "ld": os.environ.get("LD_LIBRARY_PATH")}, fh)
attempt = 0
counter = os.environ.get("FAKE_TOR_COUNT")
if counter:
    attempt = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(attempt + 1))
if mode == "exit":
    print("[err] Problem bootstrapping", flush=True)
    sys.exit(1)
if mode == "busy_once" and attempt == 0:
    print("[warn] Could not bind to 127.0.0.1:1: Address already in use", flush=True)
    sys.exit(1)
print("Bootstrapped 5% (conn): Connecting to a relay", flush=True)
if mode == "hang":
    time.sleep(60)
print("Bootstrapped 100% (done): Done", flush=True)
time.sleep(60)
'''


@pytest.fixture
def fake_tor(tmp_path, monkeypatch):
    """A TorManager whose tor is a Python script, and the script's path."""
    script = tmp_path / "fake_tor.py"
    script.write_text(FAKE_TOR, encoding="utf-8")
    monkeypatch.setenv(bundle.TOR_BIN_ENV, str(script))
    made = []

    def build():
        tor = manager.TorManager(home=str(tmp_path / "home"),
                                 argv_prefix=[sys.executable, str(script)])
        made.append(tor)
        return tor

    yield build
    for tor in made:
        tor.stop()


def test_start_returns_a_loopback_endpoint_and_reuses_it(fake_tor):
    tor = fake_tor()
    endpoint = tor.start()
    assert endpoint.startswith("socks5://127.0.0.1:")
    assert tor.start() == endpoint
    assert tor.status() == {"running": True, "endpoint": endpoint,
                            "bootstrap": "100% (done): Done"}


def test_stop_ends_the_process_and_a_new_start_makes_a_new_one(fake_tor):
    tor = fake_tor()
    tor.start()
    first = tor._proc
    tor.stop()
    assert first.poll() is not None
    assert tor.status()["running"] is False
    tor.stop()                       # idempotent
    tor.start()
    assert tor._proc is not first and tor._proc.poll() is None


def test_tor_is_told_its_port_its_owner_and_nothing_wider(fake_tor, monkeypatch, tmp_path):
    dump = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_TOR_ARGV", str(dump))
    tor = fake_tor()
    endpoint = tor.start()
    seen = json.loads(dump.read_text())["argv"]
    pairs = {a: seen[i + 1] for i, a in enumerate(seen[:-1]) if a.startswith("--")}
    assert pairs["--SocksPort"] == "127.0.0.1:" + endpoint.rsplit(":", 1)[1]
    assert pairs["--__OwningControllerProcess"] == str(os.getpid())
    assert pairs["--ControlPort"] == "0"


def test_an_early_exit_reports_what_tor_said(fake_tor, monkeypatch):
    monkeypatch.setenv("FAKE_TOR_MODE", "exit")
    with pytest.raises(manager.TorError, match="(?s)exited.*Problem bootstrapping"):
        fake_tor().start()


def test_a_port_that_was_taken_is_retried_on_another(fake_tor, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_TOR_MODE", "busy_once")
    monkeypatch.setenv("FAKE_TOR_COUNT", str(tmp_path / "count"))
    tor = fake_tor()
    assert tor.start().startswith("socks5://")
    assert (tmp_path / "count").read_text() == "2"


def test_a_failure_that_is_not_a_busy_port_is_not_retried(fake_tor, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_TOR_MODE", "exit")
    monkeypatch.setenv("FAKE_TOR_COUNT", str(tmp_path / "count"))
    with pytest.raises(manager.TorError):
        fake_tor().start()
    assert (tmp_path / "count").read_text() == "1"


def test_a_bootstrap_that_never_finishes_times_out_and_kills_tor(fake_tor, monkeypatch):
    monkeypatch.setenv("FAKE_TOR_MODE", "hang")
    monkeypatch.setattr(manager, "BOOTSTRAP_TIMEOUT_SECONDS", 1)
    tor = fake_tor()
    with pytest.raises(manager.TorError, match="did not finish bootstrapping"):
        tor.start()
    assert tor._proc is None


def test_a_missing_loader_is_explained_not_reported_as_a_missing_file(
        fake_tor, monkeypatch, tmp_path):
    def no_loader(*args, **kwargs):
        raise OSError(errno.ENOENT, "No such file or directory", "/x/tor")

    monkeypatch.setattr(manager.subprocess, "Popen", no_loader)
    with pytest.raises(manager.TorError, match=r"musl.*" + bundle.TOR_BIN_ENV):
        fake_tor().start()


def test_an_override_that_is_not_a_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(bundle.TOR_BIN_ENV, str(tmp_path / "missing"))
    with pytest.raises(manager.TorError, match=bundle.TOR_BIN_ENV):
        manager.TorManager(home=str(tmp_path)).ensure_installed()


# ---- install, with a fake archive instead of a download ---------------------------------


def _bundle_tarball(extra=()):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as writer:
        for name, data in [("tor/tor", b"#!/bin/sh\n"), ("tor/libevent-2.1.so.7", b"lib"),
                           ("tor/pluggable_transports/lyrebird", b"pt"),
                           ("data/geoip", b"g4"), ("data/geoip6", b"g6"), *extra]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            writer.addfile(info, io.BytesIO(data))
    return raw.getvalue()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _install_fixture(monkeypatch, tmp_path, payload, os_name="linux"):
    monkeypatch.delenv(bundle.TOR_BIN_ENV, raising=False)
    chosen = bundle.Bundle(os_name=os_name, arch="x86_64", version="0.0.1",
                           sha256=hashlib.sha256(_bundle_tarball()).hexdigest())
    monkeypatch.setattr(bundle, "select", lambda *a, **k: chosen)
    calls = []

    def opener(url, timeout):
        calls.append(url)
        return _Response(payload)

    return manager.TorManager(home=str(tmp_path / "home"), opener=opener), chosen, calls


def test_install_unpacks_only_what_tor_needs_and_downloads_once(monkeypatch, tmp_path):
    tor, chosen, calls = _install_fixture(monkeypatch, tmp_path, _bundle_tarball())
    binary = tor.ensure_installed()
    root = tmp_path / "home" / chosen.folder
    assert binary == str(root / "tor" / "tor")
    assert sorted(os.listdir(root / "tor")) == ["libevent-2.1.so.7", "tor"]
    assert sorted(os.listdir(root / "data")) == ["geoip", "geoip6"]
    assert (root / ".complete").read_text().strip() == chosen.sha256
    if os.name != "nt":
        assert os.access(binary, os.X_OK)
    assert tor.ensure_installed() == binary
    assert len(calls) == 1
    assert [p.name for p in (tmp_path / "home").iterdir()] == [chosen.folder]


def test_install_passes_the_bundled_geoip_and_library_path_to_tor(monkeypatch, tmp_path):
    tor, chosen, _ = _install_fixture(monkeypatch, tmp_path, _bundle_tarball())
    binary = tor.ensure_installed()
    env = tor.environment(binary)
    assert env["LD_LIBRARY_PATH"].split(os.pathsep)[0] == os.path.dirname(binary)
    assert tor._geoip[0].endswith(os.path.join("data", "geoip"))


def test_a_tampered_download_is_refused_and_leaves_nothing_behind(monkeypatch, tmp_path):
    tampered = _bundle_tarball(extra=[("tor/extra", b"evil")])
    tor, chosen, _ = _install_fixture(monkeypatch, tmp_path, tampered)
    with pytest.raises(manager.TorError, match="expected " + chosen.sha256):
        tor.ensure_installed()
    assert list((tmp_path / "home").iterdir()) == []


def test_a_download_that_fails_names_the_url(monkeypatch, tmp_path):
    tor, chosen, _ = _install_fixture(monkeypatch, tmp_path, b"")

    def broken(url, timeout):
        raise OSError("network down")

    tor._opener = broken
    with pytest.raises(manager.TorError, match="network down"):
        tor.ensure_installed()


def test_an_older_install_is_removed_after_a_new_one(monkeypatch, tmp_path):
    tor, chosen, _ = _install_fixture(monkeypatch, tmp_path, _bundle_tarball())
    old = tmp_path / "home" / "0.0.0-linux-x86_64"
    old.mkdir(parents=True)
    (tmp_path / "home" / "data").mkdir()
    tor.ensure_installed()
    assert not old.exists()
    assert (tmp_path / "home" / "data").exists()


def test_the_macos_binaries_are_ad_hoc_signed_after_unpacking(monkeypatch, tmp_path):
    signed = []

    def record(cmd, **kwargs):
        signed.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(manager.subprocess, "run", record)
    tor, chosen, _ = _install_fixture(monkeypatch, tmp_path, _bundle_tarball(), os_name="macos")
    tor.ensure_installed()
    assert sorted(os.path.basename(c[-1]) for c in signed) == ["libevent-2.1.so.7", "tor"]
    assert all(c[:4] == ["codesign", "--force", "--sign", "-"] for c in signed)


def test_a_failed_signature_aborts_the_install(monkeypatch, tmp_path):
    def refuse(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="no identity")

    monkeypatch.setattr(manager.subprocess, "run", refuse)
    tor, chosen, _ = _install_fixture(monkeypatch, tmp_path, _bundle_tarball(), os_name="macos")
    with pytest.raises(manager.TorError, match="no identity"):
        tor.ensure_installed()
    assert list((tmp_path / "home").iterdir()) == []


def test_tor_lives_beside_the_code_unless_told_otherwise(monkeypatch, tmp_path):
    monkeypatch.delenv(manager.TOR_HOME_ENV, raising=False)
    here = os.path.dirname(os.path.abspath(manager.__file__))
    assert manager.TorManager()._home == os.path.join(here, "runtime")
    monkeypatch.setenv(manager.TOR_HOME_ENV, str(tmp_path / "elsewhere"))
    assert manager.TorManager()._home == str(tmp_path / "elsewhere")


def test_the_default_home_is_ignored_by_git():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(manager.__file__))))
    with open(os.path.join(root, ".gitignore"), encoding="utf-8") as fh:
        assert "/server/tor/runtime/" in fh.read().splitlines()
