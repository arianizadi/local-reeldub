"""Temporary, loopback-only HTTPS egress for an Instagram extraction."""
import errno
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import re
import select
import socket
from threading import Thread

DOMAINS = ("instagram.com", "cdninstagram.com", "fbcdn.net")
PRIVATE_PROXY = ("127.0.0.1", 18322)


def allowed_host(host):
    return bool(re.fullmatch(r"[a-z0-9.-]{1,253}", host, re.ASCII)) and any(
        host == domain or host.endswith("." + domain) for domain in DOMAINS)


def connect_public(host):
    family = socket.AF_INET if os.getenv("REELDUB_INTAKE_IPV4_ONLY") == "1" else socket.AF_UNSPEC
    records = socket.getaddrinfo(host, 443, family=family, type=socket.SOCK_STREAM)
    if not records or any(not ip_address(row[4][0]).is_global or ip_address(row[4][0]).is_multicast for row in records):
        raise ValueError("Media destination is not public.")
    # Connect to the vetted address directly. TLS hostname verification remains in the downloader.
    for family, kind, proto, _, endpoint in records:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(12)
            sock.connect(endpoint)
            return sock
        except OSError:
            sock.close()
    raise ConnectionError("Media host unavailable.")


class MediaTunnel(BaseHTTPRequestHandler):
    timeout = 30

    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        host, _, port = self.path.lower().rpartition(":")
        if port != "443" or not allowed_host(host):
            self.send_error(403)
            return
        try:
            upstream = connect_public(host)
        except (ValueError, OSError):
            self.send_error(502)
            return
        with upstream:
            self.send_response(200)
            self.end_headers()
            self.connection.settimeout(30)
            peers = (self.connection, upstream)
            try:
                while True:
                    ready, _, _ = select.select(peers, [], [], 30)
                    if not ready:
                        return
                    for src in ready:
                        data = src.recv(65536)
                        if not data:
                            return
                        (upstream if src is self.connection else self.connection).sendall(data)
            except OSError:
                return


@contextmanager
def instagram_egress():
    if os.getenv("REELDUB_INTAKE_FIXED_PROXY") == "1":
        yield f"http://{PRIVATE_PROXY[0]}:{PRIVATE_PROXY[1]}"
        return
    ports = os.environ.get("REELDUB_INTAKE_PROXY_PORTS", "0").split(",")
    for port in ports:
        try:
            listener = ThreadingHTTPServer(("127.0.0.1", int(port)), MediaTunnel)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    else:
        raise ConnectionError("All media proxy ports are in use.")
    with listener as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    with ThreadingHTTPServer(PRIVATE_PROXY, MediaTunnel) as server:
        server.serve_forever()
