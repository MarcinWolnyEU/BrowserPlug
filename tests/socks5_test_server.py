"""Minimal SOCKS5 server for tests — no external dependencies.

Implements just enough of RFC 1928 to prove BrowserPlug's per-tab proxy routing without
Tor or any third-party service: the no-auth handshake plus the CONNECT command for
ATYP domain / IPv4. The key trick: a connecting client's requested HOSTNAME is
"resolved" BY THIS SERVER (not the client) to a fixed loopback target. So a browser
tab that routes through this proxy can reach a host the client could never resolve
directly (e.g. an *.invalid name) — the local stand-in for ".onion resolves only at
the proxy", and a proof that DNS happened remotely with no local leak.

`requested_hosts` records every hostname the proxy was asked to reach, so a test can
assert the traffic genuinely went through the proxy (remote DNS) rather than direct.
"""

import socket
import struct
import threading
import select


class Socks5TestServer:
    def __init__(self, connect_host="127.0.0.1"):
        # Every CONNECT is dialed to this host (the proxy's "resolver"), keeping the
        # requested port. Tests point a tab at http://<unresolvable>:<real_port>/.
        self._connect_host = connect_host
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self.port = self._sock.getsockname()[1]
        self.requested_hosts = []   # hostnames the proxy was asked to reach
        self._stop = threading.Event()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _accept_loop(self):
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    @staticmethod
    def _recvn(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("short read")
            buf += chunk
        return buf

    def _handle(self, client):
        upstream = None
        try:
            ver, nmethods = self._recvn(client, 2)
            if ver != 5:
                return
            self._recvn(client, nmethods)          # discard offered auth methods
            client.sendall(b"\x05\x00")            # choose "no authentication"

            ver, cmd, _rsv, atyp = self._recvn(client, 4)
            if atyp == 1:                          # IPv4
                self._recvn(client, 4)
            elif atyp == 3:                        # domain name
                ln = self._recvn(client, 1)[0]
                host = self._recvn(client, ln).decode("latin-1")
                self.requested_hosts.append(host)
            elif atyp == 4:                        # IPv6 (read + ignore)
                self._recvn(client, 16)
            else:
                client.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            port = struct.unpack("!H", self._recvn(client, 2))[0]

            if cmd != 1:                           # only CONNECT is supported
                client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            try:
                upstream = socket.create_connection((self._connect_host, port), timeout=5)
            except OSError:
                client.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")  # connection refused
                return
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")      # success
            self._relay(client, upstream)
        except Exception:
            pass
        finally:
            for s in (client, upstream):
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass

    def _relay(self, a, b):
        a.setblocking(False)
        b.setblocking(False)
        pair = [a, b]
        while not self._stop.is_set():
            try:
                readable, _, errored = select.select(pair, [], pair, 0.5)
            except (OSError, ValueError):
                return
            if errored:
                return
            for s in readable:
                other = b if s is a else a
                try:
                    data = s.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    return
                if not data:
                    return
                try:
                    other.sendall(data)
                except OSError:
                    return

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass
