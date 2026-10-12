"""Which Tor Expert Bundle belongs on this machine, and how to unpack it safely.

Pure policy: no network, no process, no state directory. `server/tor/manager.py` does the
I/O and calls in here for every decision, so each one is unit-tested without a download
(`tests/unit/test_tor_bundle.py`). `.tmp/dev-docs/tor.md` has the why.
"""

import hashlib
import os
import platform
import stat
import sys
from dataclasses import dataclass

# The release whose archive the table below pins. Bumping it means re-reading the hashes
# from that release's sha256sums-signed-build.txt, nothing else (.tmp/dev-docs/tor.md, "Upgrading").
BUNDLE_VERSION = "15.0.24"

ARCHIVE_URL = ("https://archive.torproject.org/tor-package-archive/torbrowser/"
               "{version}/tor-expert-bundle-{os_name}-{arch}-{version}.tar.gz")

# SHA-256 of each tarball. The code carries the hashes, so a changed or replaced archive
# on the mirror is refused rather than run. The archives also ship a detached .asc, but
# the stdlib cannot check a PGP signature; pinning a hash read from the signed list is the
# stdlib-only equivalent.
BUNDLE_SHA256 = {
    ("windows", "x86_64"): "e9dc6ccc93cd6afa507193f4de284d6424233ff5102155cd2c94b259e8a22b65",
    ("windows", "i686"): "7c2755b09876ebc6c2e2d2d1d3279b2be3e9beec35ff0ca2e7df4c1abcad1ae4",
    ("linux", "x86_64"): "8e012ec6815d7899cb64011582e2dade88e74119c6661068a2a3252de0ccd7f2",
    ("linux", "i686"): "7537fea3478d05b8af25d7f8199c031b281f7015c32bb4177bef71f8e5100d9b",
    ("macos", "x86_64"): "8acb0b590f6be34084dcb6d84009ac0c61cc7c5261b7a19d2ab94845aa9bd5b6",
    ("macos", "aarch64"): "d47afd04b6c751129978390ad003d74ac8b88adfbb939350f0f89999e6570644",
}

# Set to the path of a `tor` you installed yourself (a distro package, for instance) and
# the server uses it and downloads nothing. The way out on a platform with no bundle.
TOR_BIN_ENV = "BROWSERPLUG_TOR_BIN"

_OS_NAMES = {"win32": "windows", "linux": "linux", "darwin": "macos"}
_ARCH_NAMES = {
    "amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64",
    "arm64": "aarch64", "aarch64": "aarch64",
    "i386": "i686", "i686": "i686", "x86": "i686",
}


class UnsupportedPlatform(Exception):
    """No Tor Expert Bundle exists for this machine. `str(error)` says what to do."""


@dataclass(frozen=True)
class Bundle:
    os_name: str
    arch: str
    version: str
    sha256: str

    @property
    def url(self):
        return ARCHIVE_URL.format(version=self.version, os_name=self.os_name, arch=self.arch)

    @property
    def folder(self):
        """The directory name this bundle unpacks into under the tor home."""
        return f"{self.version}-{self.os_name}-{self.arch}"

    @property
    def binary(self):
        """Path of the tor executable inside the unpacked folder, '/'-separated."""
        return "tor/tor.exe" if self.os_name == "windows" else "tor/tor"


def select(system=None, machine=None, version=BUNDLE_VERSION):
    """The bundle for `system` (a `sys.platform` value) and `machine` (a
    `platform.machine()` value); the running machine when omitted."""
    system = sys.platform if system is None else system
    machine = platform.machine() if machine is None else machine
    os_name = _OS_NAMES.get("linux" if system.startswith("linux") else system)
    arch = _ARCH_NAMES.get(machine.lower())
    if os_name is None or arch is None:
        raise UnsupportedPlatform(
            f"No Tor bundle for {system}/{machine}. Install tor yourself and point "
            f"{TOR_BIN_ENV} at it.")
    sha256 = BUNDLE_SHA256.get((os_name, arch))
    if sha256 is None:
        raise UnsupportedPlatform(
            f"The Tor Project publishes no {os_name} {arch} bundle. Install tor with your "
            f"package manager and point {TOR_BIN_ENV} at it.")
    return Bundle(os_name=os_name, arch=arch, version=version, sha256=sha256)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_wanted(name):
    """Whether an archive member is worth unpacking: the tor binary with the libraries it
    loads from its own folder (`tor/*`, no pluggable transports) and the two GeoIP files.
    The rest of the bundle (about 60 MB of bridge transports, docs, debug symbols) is
    never used by a plain SOCKS client."""
    if name in ("data/geoip", "data/geoip6"):
        return True
    parts = name.split("/")
    return len(parts) == 2 and parts[0] == "tor" and bool(parts[1])


def wanted_members(archive):
    """The members of an open `tarfile.TarFile` to unpack. Raises ValueError for one that
    would escape the target folder or is not a plain file, instead of skipping it: a
    hash-checked archive that nonetheless carries a path traversal is not one to half-trust."""
    members = []
    for member in archive.getmembers():
        name = member.name
        if os.path.isabs(name) or ".." in name.replace("\\", "/").split("/"):
            raise ValueError(f"unsafe path in the Tor bundle: {name!r}")
        if member.isdir() or not is_wanted(name):
            continue
        if not member.isreg():
            raise ValueError(f"the Tor bundle member {name!r} is not a regular file")
        members.append(member)
    return members


def runtime_files(folder_names):
    """Names under `tor/` that must be marked executable or signed: the binary and every
    shared library next to it, which the binary loads by a path relative to itself."""
    return [n for n in folder_names
            if n.startswith("tor/") and (n.endswith((".so", ".dylib", ".dll"))
                                         or ".so." in n or n in ("tor/tor", "tor/tor.exe"))]


def make_executable(path):
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def library_environment(binary, environ, os_name):
    """The environment to run the bundled `binary` in.

    The Linux tor is linked without a RUNPATH, so the libevent (and OpenSSL) shipped next
    to it are found only through LD_LIBRARY_PATH, which is how Tor Browser's own launcher
    starts it; a bare exec fails with "libevent-2.1.so.7: cannot open shared object file"
    wherever the distro has no libevent. The bundled folder goes first so the OpenSSL tor
    was built against wins over a different system one. macOS finds its dylib through
    @executable_path and Windows has no sidecar DLL, so both pass through unchanged. An
    `os_name` of None (a tor the user supplied) changes nothing."""
    env = dict(environ)
    if os_name == "linux":
        existing = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = os.path.dirname(binary) + (os.pathsep + existing if existing else "")
    return env


def tor_arguments(*, socks_port, data_dir, owner_pid, geoip_file=None, geoip6_file=None):
    """Arguments for `tor` as a loopback-only SOCKS5 client with no config file.

    The SOCKS port binds 127.0.0.1 and nothing wider, with no ControlPort and no relay or
    exit role. `__OwningControllerProcess` makes tor exit when the server that started it
    is gone, however it went, so a crash cannot leave a daemon behind."""
    args = [
        "--ignore-missing-torrc", "-f", os.devnull,
        "--SocksPort", f"127.0.0.1:{int(socks_port)}",
        "--ControlPort", "0",
        "--DataDirectory", data_dir,
        "--Log", "notice stdout",
        "--__OwningControllerProcess", str(int(owner_pid)),
    ]
    if geoip_file:
        args += ["--GeoIPFile", geoip_file]
    if geoip6_file:
        args += ["--GeoIPv6File", geoip6_file]
    return args

