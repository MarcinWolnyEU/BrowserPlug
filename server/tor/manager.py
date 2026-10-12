"""Installs the official Tor client and runs it as a loopback SOCKS5 proxy.

The hook for `proxy="tor"`: `TorManager.start()` returns a `socks5://127.0.0.1:<port>`
endpoint for the extension's per-tab routing, installing the right Tor Expert Bundle first
if its home (`DEFAULT_HOME` beside this file, or `BROWSERPLUG_TOR_HOME`) has none. Every
decision (which bundle, which hash, which arguments, which archive members) lives in
`server/tor/bundle.py`; this file only does the I/O.

Blocking by design: callers on the event loop wrap it in `asyncio.to_thread`. `.tmp/dev-docs/tor.md`
has the contract and the platform notes.
"""

import atexit
import collections
import errno
import logging
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import urllib.request

from server.tor import bundle as tor_bundle

log = logging.getLogger("browserplug.tor")

# Where the downloaded bundle and tor's own data live. Beside the code, in a folder git
# ignores, so the whole client is one visible directory that is deleted by deleting it.
# BROWSERPLUG_TOR_HOME moves it (the Docker test mounts the checkout read-only).
TOR_HOME_ENV = "BROWSERPLUG_TOR_HOME"
DEFAULT_HOME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime")

DOWNLOAD_TIMEOUT_SECONDS = 60
# A cold start fetches the consensus and descriptors: about 33 s measured on Windows.
BOOTSTRAP_TIMEOUT_SECONDS = 180
BOOTSTRAP_DONE = "Bootstrapped 100%"
START_ATTEMPTS = 3
_PORT_BUSY_MARKERS = ("Address already in use", "Could not bind", "address already in use")
_LOG_TAIL_LINES = 40


class TorError(Exception):
    """Tor could not be installed or started. `str(error)` is shown to the caller."""


def _free_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


_shared = None
_shared_lock = threading.Lock()


def shared():
    """The one TorManager of this process, created on first use."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = TorManager()
        return _shared


class TorManager:
    def __init__(self, home=None, argv_prefix=None, opener=None):
        # `argv_prefix` replaces the tor binary in the command line and `opener` replaces
        # urllib's urlopen: both exist so tests run a stand-in without a download.
        self._home = home or (os.environ.get(TOR_HOME_ENV) or "").strip() or DEFAULT_HOME
        self._argv_prefix = list(argv_prefix) if argv_prefix else None
        self._opener = opener or urllib.request.urlopen
        self._lock = threading.RLock()
        self._proc = None
        self._endpoint = None
        self._log_tail = collections.deque(maxlen=_LOG_TAIL_LINES)
        self._progress = ""
        self._geoip = (None, None)
        self._bundled_os = None
        atexit.register(self.stop)

    # ---- install -------------------------------------------------------------------

    def ensure_installed(self):
        """Path of a tor binary, installing the bundle for this machine if needed.
        BROWSERPLUG_TOR_BIN wins and downloads nothing."""
        with self._lock:
            override = (os.environ.get(tor_bundle.TOR_BIN_ENV) or "").strip()
            if override:
                if not os.path.isfile(override):
                    raise TorError(f"{tor_bundle.TOR_BIN_ENV} points at {override!r}, "
                                   "which is not a file.")
                self._geoip, self._bundled_os = (None, None), None
                return override
            try:
                bundle = tor_bundle.select()
            except tor_bundle.UnsupportedPlatform as exc:
                raise TorError(str(exc)) from exc
            target = os.path.join(self._home, bundle.folder)
            binary = os.path.join(target, *bundle.binary.split("/"))
            if not os.path.isfile(os.path.join(target, ".complete")):
                self._install(bundle, target)
            self._geoip = (os.path.join(target, "data", "geoip"),
                           os.path.join(target, "data", "geoip6"))
            self._bundled_os = bundle.os_name
            return binary

    def _install(self, bundle, target):
        os.makedirs(self._home, exist_ok=True)
        staging = tempfile.mkdtemp(prefix="unpack-", dir=self._home)
        try:
            archive_path = os.path.join(staging, "bundle.tar.gz")
            self._download(bundle, archive_path)
            unpacked = os.path.join(staging, "out")
            self._unpack(archive_path, unpacked)
            if bundle.os_name == "macos":
                self._sign_for_apple_silicon(unpacked)
            with open(os.path.join(unpacked, ".complete"), "w", encoding="utf-8") as fh:
                fh.write(bundle.sha256 + "\n")
            shutil.rmtree(target, ignore_errors=True)
            os.replace(unpacked, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self._remove_older_installs(keep=bundle.folder)
        log.info("installed Tor %s into %s", bundle.version, target)

    def _download(self, bundle, destination):
        log.info("downloading %s", bundle.url)
        try:
            with self._opener(bundle.url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, \
                    open(destination, "wb") as out:
                shutil.copyfileobj(response, out)
        except OSError as exc:
            raise TorError(f"Could not download the Tor bundle from {bundle.url}: {exc}") from exc
        got = tor_bundle.sha256_of(destination)
        if got != bundle.sha256:
            raise TorError(f"The Tor bundle from {bundle.url} has SHA-256 {got}, expected "
                           f"{bundle.sha256}. It was not unpacked or run.")

    @staticmethod
    def _unpack(archive_path, destination):
        os.makedirs(destination)
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                members = tor_bundle.wanted_members(archive)
                archive.extractall(destination, members=members, filter="data")
        except (tarfile.TarError, ValueError, OSError) as exc:
            raise TorError(f"Could not unpack the Tor bundle: {exc}") from exc
        names = [m.name for m in members]
        for name in tor_bundle.runtime_files(names):
            tor_bundle.make_executable(os.path.join(destination, *name.split("/")))

    @staticmethod
    def _sign_for_apple_silicon(unpacked):
        """The macOS arm64 binaries in the bundle carry no code signature, and arm64 macOS
        kills an unsigned binary on exec. An ad-hoc signature is enough. Untested on
        Apple hardware (.tmp/dev-docs/tor.md)."""
        folder = os.path.join(unpacked, "tor")
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            try:
                subprocess.run(["codesign", "--force", "--sign", "-", path], check=True,
                               capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.SubprocessError) as exc:
                detail = getattr(exc, "stderr", "") or exc
                raise TorError(f"Could not ad-hoc sign {name} for macOS: {detail}") from exc

    def _remove_older_installs(self, keep):
        try:
            for entry in os.listdir(self._home):
                path = os.path.join(self._home, entry)
                if entry != keep and entry[:1].isdigit() and os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

    # ---- run -----------------------------------------------------------------------

    def start(self):
        """Start tor if it is not running; return its `socks5://127.0.0.1:<port>`
        endpoint once it has bootstrapped. Raises TorError."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self._endpoint:
                return self._endpoint
            self._reset()
            binary = self.ensure_installed()
            failure = None
            for _ in range(START_ATTEMPTS):
                try:
                    return self._spawn(binary, _free_loopback_port())
                except TorError as exc:
                    failure = exc
                    self.stop()
                    if not any(m in str(exc) for m in _PORT_BUSY_MARKERS):
                        break
            raise failure

    def _spawn(self, binary, port):
        data_dir = os.path.join(self._home, "data")
        os.makedirs(data_dir, exist_ok=True)
        args = tor_bundle.tor_arguments(
            socks_port=port, data_dir=data_dir, owner_pid=os.getpid(),
            geoip_file=self._geoip[0], geoip6_file=self._geoip[1])
        argv = (self._argv_prefix or [binary]) + args
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                bufsize=1, cwd=self._home, creationflags=flags,
                env=tor_bundle.library_environment(binary, os.environ, self._bundled_os))
        except OSError as exc:
            hint = ""
            if exc.errno == errno.ENOENT and os.path.exists(binary):
                # The file is there, so it is the dynamic loader the kernel cannot find.
                hint = (" The file exists, so the system lacks the glibc loader it needs: "
                        "this is a musl system (Alpine) and the bundle is built for glibc. "
                        f"Install tor with your package manager and set {tor_bundle.TOR_BIN_ENV}.")
            raise TorError(f"Could not start tor ({binary}): {exc}.{hint}") from exc
        self._proc = proc
        ready, settled = threading.Event(), threading.Event()
        threading.Thread(target=self._read_output, args=(proc, ready, settled), daemon=True,
                         name="browserplug-tor-log").start()
        settled.wait(BOOTSTRAP_TIMEOUT_SECONDS)
        if not ready.is_set():
            # End of output means the process is ending; give its exit status a moment to
            # land so the error says "exited" rather than "did not finish".
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                pass
        if not ready.is_set() or proc.poll() is not None:
            tail = "\n".join(self._log_tail)
            state = "exited" if proc.poll() is not None else "did not finish bootstrapping"
            raise TorError(f"tor {state}. Last output:\n{tail}")
        self._endpoint = f"socks5://127.0.0.1:{port}"
        return self._endpoint

    def _read_output(self, proc, ready, settled):
        """Drain tor's output for as long as it runs: a full pipe would stall tor. `ready`
        means bootstrap finished; `settled` is set then or when the process ends."""
        try:
            for line in proc.stdout:
                line = line.rstrip()
                self._log_tail.append(line)
                if "Bootstrapped" in line:
                    self._progress = line.split("Bootstrapped", 1)[1].strip()
                if BOOTSTRAP_DONE in line:
                    ready.set()
                    settled.set()
        finally:
            settled.set()
            proc.stdout.close()

    def _reset(self):
        self._endpoint = None
        self._progress = ""
        self._log_tail.clear()

    def stop(self):
        with self._lock:
            proc, self._proc, self._endpoint = self._proc, None, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)

    def environment(self, binary):
        """The environment `binary` runs in (the smoke test's `ldd` uses the same one)."""
        return tor_bundle.library_environment(binary, os.environ, self._bundled_os)

    def status(self):
        running = self._proc is not None and self._proc.poll() is None
        return {"running": running, "endpoint": self._endpoint if running else None,
                "bootstrap": self._progress}
