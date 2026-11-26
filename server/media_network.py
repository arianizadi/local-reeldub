"""Public HTTP(S) media egress. Validate every DNS answer and connect to that address."""
import errno
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlsplit

from .video_url import public_address, public_hostname, video_url
import select
import socket
from threading import Thread

PRIVATE_PROXY = ("127.0.0.1", 18322)


def connect_public(host, port):
    host = public_hostname(host)
    if port not in (80, 443):
        raise ValueError("Unsupported media port.")
    # Resolve both families even on IPv4-only deployments. A mixed answer fails closed.
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not records or any(not public_address(row[4][0]) for row in records):
        raise ValueError("Media destination is not public.")
    if os.getenv("REELDUB_INTAKE_IPV4_ONLY") == "1":
        records = [row for row in records if row[0] == socket.AF_INET]
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
        try:
            dest = urlsplit(video_url("https://" + self.path, submission=False))
            if self.path.rsplit(":", 1)[-1] != "443" or dest.path != "/" or dest.query or dest.fragment:
                raise ValueError()
            upstream = connect_public(dest.hostname, 443)
        except ValueError:
            self.send_error(403)
            return
        except OSError:
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

    def forward(self):
        # Always use a fresh upstream connection. Strip hop-by-hop headers, including
        # those nominated by Connection, and derive Host from the validated URL.
        upstream = None
        try:
            dest = urlsplit(video_url(self.path, submission=False))
            if dest.scheme != "http" or dest.fragment or "Transfer-Encoding" in self.headers:
                raise ValueError()
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
                raise ValueError()
            size = int(lengths[0]) if lengths else 0
            if size > 2 * 1024**2 or (size and self.command != "POST"):
                raise ValueError()
            hop = {"host", "connection", "content-length", "keep-alive", "proxy-connection",
                   "proxy-authorization", "proxy-authenticate", "te", "trailer", "transfer-encoding", "upgrade"}
            hop.update(name.strip().lower() for name in self.headers.get("Connection", "").split(","))
            headers = {k: v for k, v in self.headers.items() if k.lower() not in hop}
            upstream = HTTPConnection(dest.hostname, 80, timeout=30)
            upstream.sock = connect_public(dest.hostname, 80)
            path = dest.path + ("?" + dest.query if dest.query else "")
            body = self.rfile.read(size) if size else None
            if size and len(body) != size:
                raise ValueError()
            upstream.request(self.command, path, body=body, headers=headers)
            reply = upstream.getresponse()
            self.send_response_only(reply.status)
            hop.update(name.strip().lower() for name in reply.getheader("Connection", "").split(","))
            for name, value in reply.getheaders():
                if name.lower() not in hop:
                    self.send_header(name, value)
            length = reply.getheader("Content-Length") if self.command == "HEAD" else reply.length
            if length is not None and str(length).isdigit():
                self.send_header("Content-Length", str(length))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                while chunk := reply.read(65536):
                    self.wfile.write(chunk)
        except (OSError, HTTPException, ValueError):
            if not self.wfile.closed and not self._headers_sent:
                self.send_error(502)
        finally:
            if upstream:
                upstream.close()
            self.close_connection = True

    _headers_sent = False

    def end_headers(self):
        super().end_headers()
        self._headers_sent = True

    do_GET = do_HEAD = do_POST = forward


@contextmanager
def media_egress():
    if os.getenv("REELDUB_INTAKE_FIXED_PROXY") == "1":
        host = os.getenv("REELDUB_INTAKE_PROXY_HOST", PRIVATE_PROXY[0])
        yield f"http://{host}:{PRIVATE_PROXY[1]}"
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
    address = (os.getenv("REELDUB_PROXY_LISTEN", PRIVATE_PROXY[0]), PRIVATE_PROXY[1])
    with ThreadingHTTPServer(address, MediaTunnel) as server:
        server.serve_forever()
