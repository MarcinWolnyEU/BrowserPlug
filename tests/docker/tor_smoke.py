"""End-to-end check of server/tor on a real Linux, run INSIDE a container by
tests/docker/run_tor_linux.py (.tmp/dev-docs/tor.md, "Testing on Linux").

Stdlib only. It downloads the real Tor Expert Bundle, so it needs the network, and it
asserts rather than skips: any failed step is a non-zero exit with the step named.
"""

import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path[:0] = ["/repo", "/repo/shared-py"]
os.environ["BROWSERPLUG_STATE_DIR"] = tempfile.mkdtemp(prefix="bp-state-")
os.environ["BROWSERPLUG_TOR_HOME"] = tempfile.mkdtemp(prefix="bp-tor-")

from server.tor import bundle as tor_bundle  # noqa: E402
from server.tor.manager import TorManager  # noqa: E402

TOR_ONION = "2gzyxa5ihm7nsggfxnu52rck2vv4rvmdlkiu3zzui5du4xyclen53wid.onion"
ONION_IMAGES = [
    "/static/images/home/png/block-trackers@3x.png",
    "/static/images/home/png/surveillance@3x.png",
    "/static/images/home/png/fingerprinting@3x.png",
    "/static/images/home/png/encryption@3x.png",
    "/static/images/home/png/browse-freely@3x.png",
    "/static/images/circle-pattern.png",
]
# torquests stalled one or two of these six for 35 to 90 s; tor answers all in under 5 s.
IMAGE_DEADLINE_SECONDS = 30


def step(name):
    print(f"\n=== {name}", flush=True)


def check(condition, message):
    if not condition:
        print(f"FAIL: {message}", flush=True)
        sys.exit(1)
    print(f"ok: {message}", flush=True)


def socks_fetch(endpoint, host, path, tls, timeout=60):
    """GET http(s)://host/path through a SOCKS5 proxy, the name resolved by the proxy."""
    proxy_host, proxy_port = endpoint.rsplit("//", 1)[1].split(":")
    sock = socket.create_connection((proxy_host, int(proxy_port)), timeout=timeout)
    sock.sendall(b"\x05\x01\x00")
    assert sock.recv(2) == b"\x05\x00", "SOCKS5 greeting refused"
    name = host.encode()
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name
                 + struct.pack(">H", 443 if tls else 80))
    head = sock.recv(4)
    if len(head) < 4 or head[1] != 0:
        raise OSError(f"SOCKS5 connect failed, reply {head!r}")
    sock.recv({1: 6, 4: 18}.get(head[3], 6))
    if tls:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    chunks = []
    while True:
        data = sock.recv(65536)
        if not data:
            break
        chunks.append(data)
    sock.close()
    raw = b"".join(chunks)
    header, _, body = raw.partition(b"\r\n\r\n")
    return int(header.split(b" ", 2)[1]), body


def process_state(pid):
    """The kernel's one-letter state for `pid`, or None when it is gone. 'Z' is a zombie:
    exited but not yet reaped, which happens here because this script is the container's
    PID 1 and an orphan is reparented to it."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return None


def alive(pid):
    return process_state(pid) not in (None, "Z")


def main():
    step("platform")
    selected = tor_bundle.select()
    print(f"{sys.platform} {os.uname().machine} -> {selected.folder}", flush=True)
    check(selected.os_name == "linux", "the bundle selected is the Linux one")

    step("install (download, hash check, unpack)")
    manager = TorManager()
    started = time.time()
    binary = manager.ensure_installed()
    print(f"{binary}, {time.time() - started:.1f}s", flush=True)
    check(os.access(binary, os.X_OK), "tor is executable")
    folder = os.path.dirname(binary)
    check(sorted(os.listdir(folder)) == sorted(
        ["tor", "libcrypto.so.3", "libevent-2.1.so.7", "libssl.so.3"]),
        f"only tor and its libraries were unpacked: {sorted(os.listdir(folder))}")
    check(os.path.isfile(os.path.join(os.path.dirname(folder), "data", "geoip")), "geoip unpacked")

    step("ldd: tor finds every library")
    bare = subprocess.run(["ldd", binary], capture_output=True, text=True).stdout
    print("without LD_LIBRARY_PATH:", [l.strip() for l in bare.splitlines() if "not found" in l],
          flush=True)
    ldd = subprocess.run(["ldd", binary], capture_output=True, text=True,
                         env=manager.environment(binary)).stdout
    print(ldd, flush=True)
    check("not found" not in ldd, "no missing shared library with the bundle's environment")
    ssl_line = next(l for l in ldd.splitlines() if "libssl.so.3" in l)
    check(os.path.dirname(binary) in ssl_line, "libssl comes from the bundle, not the system")

    step("start and bootstrap")
    started = time.time()
    endpoint = manager.start()
    print(f"{endpoint} after {time.time() - started:.1f}s", flush=True)
    check(endpoint.startswith("socks5://127.0.0.1:"), "endpoint is loopback SOCKS5")
    listeners = open("/proc/net/tcp").read()
    port_hex = f"0100007F:{int(endpoint.rsplit(':', 1)[1]):04X}"
    check(port_hex in listeners, "the SOCKS port is bound to 127.0.0.1")
    check(manager.start() == endpoint, "a second start returns the running endpoint")

    step("clearnet fetch goes through Tor")
    status, body = socks_fetch(endpoint, "check.torproject.org", "/api/ip", tls=True)
    answer = json.loads(body[body.index(b"{"):body.rindex(b"}") + 1])
    print(answer, flush=True)
    check(status == 200 and answer.get("IsTor") is True, "check.torproject.org reports IsTor")

    step("onion fetch")
    started = time.time()
    status, body = socks_fetch(endpoint, TOR_ONION, "/", tls=False)
    print(f"{status}, {len(body)} bytes, {time.time() - started:.1f}s", flush=True)
    check(status == 200 and b"Tor Project" in body, "the Tor Project onion service answered")

    step("six parallel onion fetches (the torquests stall)")
    results, slowest = {}, 0.0

    def fetch(path):
        began = time.time()
        try:
            results[path] = (socks_fetch(endpoint, TOR_ONION, path, tls=False,
                                         timeout=IMAGE_DEADLINE_SECONDS)[0],
                             time.time() - began)
        except OSError as exc:
            results[path] = (str(exc), time.time() - began)

    threads = [threading.Thread(target=fetch, args=(p,)) for p in ONION_IMAGES]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for path, (status, took) in sorted(results.items(), key=lambda kv: kv[1][1]):
        print(f"{status} {took:5.1f}s {path}", flush=True)
        slowest = max(slowest, took)
    check(all(r[0] == 200 for r in results.values()), "all six images returned 200")
    check(slowest < IMAGE_DEADLINE_SECONDS, f"slowest fetch {slowest:.1f}s < "
                                            f"{IMAGE_DEADLINE_SECONDS}s")

    step("stop")
    tor_pid = manager._proc.pid
    manager.stop()
    check(not alive(tor_pid), "tor is gone after stop()")

    step("tor exits when its owner is killed (SIGKILL, no cleanup runs)")
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import os,sys,time\nsys.path[:0]=['/repo','/repo/shared-py']\n"
         "from server.tor.manager import TorManager\nm=TorManager()\nm.start()\n"
         "print(m._proc.pid, flush=True)\ntime.sleep(600)\n"],
        stdout=subprocess.PIPE, text=True)
    orphan = int(child.stdout.readline())
    check(alive(orphan), f"the child's tor ({orphan}) is running")
    child.kill()
    child.wait()
    deadline = time.time() + 30
    while alive(orphan) and time.time() < deadline:
        time.sleep(0.5)
    print(f"state of the orphaned tor: {process_state(orphan)}", flush=True)
    check(not alive(orphan), "the orphaned tor exited by itself")
    print("\nALL STEPS PASSED", flush=True)


if __name__ == "__main__":
    main()
