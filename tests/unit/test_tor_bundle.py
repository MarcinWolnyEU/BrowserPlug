"""server/tor/bundle.py: which Tor bundle, which hash, which archive members, which
arguments. Pure policy, no download and no process (.tmp/dev-docs/tor.md)."""

import hashlib
import io
import os
import re
import tarfile

import pytest

from server.tor import bundle

SUPPORTED = [
    ("win32", "AMD64", "windows", "x86_64"),
    ("win32", "x86", "windows", "i686"),
    ("linux", "x86_64", "linux", "x86_64"),
    ("linux", "i686", "linux", "i686"),
    ("darwin", "arm64", "macos", "aarch64"),
    ("darwin", "x86_64", "macos", "x86_64"),
]


@pytest.mark.parametrize("system,machine,os_name,arch", SUPPORTED)
def test_each_supported_machine_gets_its_bundle(system, machine, os_name, arch):
    chosen = bundle.select(system, machine)
    assert (chosen.os_name, chosen.arch) == (os_name, arch)
    assert chosen.sha256 == bundle.BUNDLE_SHA256[(os_name, arch)]
    assert chosen.url == (
        "https://archive.torproject.org/tor-package-archive/torbrowser/"
        f"{bundle.BUNDLE_VERSION}/tor-expert-bundle-{os_name}-{arch}-{bundle.BUNDLE_VERSION}.tar.gz")


def test_a_linux_platform_string_with_a_suffix_still_selects_linux():
    assert bundle.select("linux2", "x86_64").os_name == "linux"


@pytest.mark.parametrize("system,machine", [
    ("linux", "aarch64"),    # the Tor Project publishes no Linux ARM bundle
    ("linux", "armv7l"),
    ("win32", "ARM64"),
    ("freebsd14", "amd64"),
    ("sunos5", "sun4v"),
])
def test_a_machine_with_no_bundle_is_refused_with_the_way_out(system, machine):
    with pytest.raises(bundle.UnsupportedPlatform) as raised:
        bundle.select(system, machine)
    assert bundle.TOR_BIN_ENV in str(raised.value)


def test_every_pinned_hash_is_a_sha256_for_a_known_platform():
    assert set(bundle.BUNDLE_SHA256) == {(o, a) for _, _, o, a in SUPPORTED}
    for digest in bundle.BUNDLE_SHA256.values():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert len(set(bundle.BUNDLE_SHA256.values())) == len(bundle.BUNDLE_SHA256)


def test_the_binary_path_and_folder_follow_the_platform():
    assert bundle.select("win32", "AMD64").binary == "tor/tor.exe"
    assert bundle.select("linux", "x86_64").binary == "tor/tor"
    assert bundle.select("darwin", "arm64").folder == f"{bundle.BUNDLE_VERSION}-macos-aarch64"


def _archive(entries):
    """An open TarFile holding (name, kind) entries; kind is 'file', 'dir' or 'link'."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as writer:
        for name, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                writer.addfile(info)
            elif kind == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                writer.addfile(info)
            else:
                data = b"x"
                info.size = len(data)
                writer.addfile(info, io.BytesIO(data))
    raw.seek(0)
    return tarfile.open(fileobj=raw, mode="r")


def test_only_the_binary_its_libraries_and_the_geoip_files_are_unpacked():
    archive = _archive([
        ("data", "dir"), ("data/geoip", "file"), ("data/geoip6", "file"),
        ("data/torrc-defaults", "file"), ("debug", "dir"), ("debug/tor", "file"),
        ("docs/tor.txt", "file"), ("tor", "dir"), ("tor/tor", "file"),
        ("tor/libssl.so.3", "file"), ("tor/pluggable_transports", "dir"),
        ("tor/pluggable_transports/lyrebird", "file"),
    ])
    assert [m.name for m in bundle.wanted_members(archive)] == [
        "data/geoip", "data/geoip6", "tor/tor", "tor/libssl.so.3"]


@pytest.mark.parametrize("name", ["../evil", "tor/../../evil", "/etc/cron.d/x", "tor\\..\\evil"])
def test_a_path_that_escapes_the_target_is_refused_not_skipped(name):
    with pytest.raises(ValueError, match="unsafe path"):
        bundle.wanted_members(_archive([("tor/tor", "file"), (name, "file")]))


def test_a_wanted_member_that_is_a_link_is_refused():
    with pytest.raises(ValueError, match="not a regular file"):
        bundle.wanted_members(_archive([("tor/tor", "link")]))


def test_runtime_files_are_the_binary_and_the_libraries_next_to_it():
    names = ["data/geoip", "tor/tor", "tor/libssl.so.3", "tor/libevent-2.1.so.7",
             "tor/libevent-2.1.7.dylib", "tor/zlib1.dll", "tor/tor.exe", "tor/README.md"]
    assert bundle.runtime_files(names) == [
        "tor/tor", "tor/libssl.so.3", "tor/libevent-2.1.so.7", "tor/libevent-2.1.7.dylib",
        "tor/zlib1.dll", "tor/tor.exe"]


def test_sha256_of_reads_a_file_in_chunks(tmp_path):
    path = tmp_path / "blob"
    path.write_bytes(b"abc" * 1_000_000)
    assert bundle.sha256_of(str(path)) == hashlib.sha256(b"abc" * 1_000_000).hexdigest()


def test_the_arguments_bind_loopback_only_and_tie_tor_to_its_owner(tmp_path):
    args = bundle.tor_arguments(socks_port=9234, data_dir=str(tmp_path), owner_pid=4321)
    pairs = {a: args[i + 1] for i, a in enumerate(args[:-1]) if a.startswith("--")}
    assert pairs["--SocksPort"] == "127.0.0.1:9234"
    assert pairs["--ControlPort"] == "0"
    assert pairs["--__OwningControllerProcess"] == "4321"
    assert pairs["--DataDirectory"] == str(tmp_path)
    assert "--GeoIPFile" not in args
    assert not any(a.startswith(("0.0.0.0", "::", "[::")) for a in args)


def test_geoip_files_are_passed_only_when_given(tmp_path):
    args = bundle.tor_arguments(socks_port=1, data_dir="d", owner_pid=1,
                                geoip_file="g4", geoip6_file="g6")
    assert args[args.index("--GeoIPFile") + 1] == "g4"
    assert args[args.index("--GeoIPv6File") + 1] == "g6"


def test_only_the_bundled_linux_tor_gets_a_library_path():
    binary = "/state/tor/15.0.24-linux-x86_64/tor/tor"
    linux = bundle.library_environment(binary, {"LD_LIBRARY_PATH": "/opt/lib", "A": "1"}, "linux")
    assert linux["LD_LIBRARY_PATH"] == "/state/tor/15.0.24-linux-x86_64/tor" + os.pathsep + "/opt/lib"
    assert linux["A"] == "1"
    assert bundle.library_environment(binary, {}, "linux")["LD_LIBRARY_PATH"] == os.path.dirname(binary)
    for other in ("macos", "windows", None):
        assert "LD_LIBRARY_PATH" not in bundle.library_environment(binary, {}, other)
