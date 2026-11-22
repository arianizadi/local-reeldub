"""Test the free connection probe and purchase restoration on a signed-out simulator.

Run from the repo root with a QA simulator UUID, optionally followed by xcodebuild flags.
For a review screenshot over HTTPS, set REELDUB_QA_TLS_CERT and REELDUB_QA_TLS_KEY
to a certificate for studio.localhost and its key, and trust its issuer on that simulator.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import threading
from uuid import UUID


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    simulator = str(UUID(sys.argv[1])).upper()
    root = Path(__file__).resolve().parents[2]
    key = secrets.token_hex(8)
    requests = []

    class Server(BaseHTTPRequestHandler):
        def do_GET(self):
            authenticated = self.headers.get("Authorization") == "Bearer " + key
            requests.append((self.command, self.path, authenticated))
            if self.path == "/v1/me" and authenticated:
                status, body = 200, {"tier": "admin", "used": 0, "library_id": "selfhost-qa"}
            else:
                status, body = 401, {"detail": "Unknown connection key."}
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_request(self, code="-", size="-"):
            if self.command != "GET":
                requests.append((self.command, self.path, False))

    with ThreadingHTTPServer(("127.0.0.1", 0), Server) as http:
        url = f"http://127.0.0.1:{http.server_port}"
        if cert := os.environ.get("REELDUB_QA_TLS_CERT"):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, os.environ["REELDUB_QA_TLS_KEY"])
            http.socket = context.wrap_socket(http.socket, server_side=True)
            url = f"https://studio.localhost:{http.server_port}"
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            for state, test in [("empty", "testFreeSelfHostConnection"), ("owned", "testRestoreSelfHostOnFreshDevice")]:
                flags = list(sys.argv[2:])
                if state == "owned" and "-resultBundlePath" in flags:
                    index = flags.index("-resultBundlePath") + 1
                    path = Path(flags[index])
                    flags[index] = str(path.with_name(path.stem + "-restored.xcresult"))
                subprocess.run(
                    [
                        "xcodebuild", "test", "-project", "ios/ReelDub.xcodeproj", "-scheme", "ReelDub",
                        "-configuration", "Debug", "-destination", f"platform=iOS Simulator,id={simulator}",
                        "-parallel-testing-enabled", "NO",
                        "-only-testing:ReelDubTests/StoreCatalogTests/testPrepareSelfHostUITestStore",
                        f"-only-testing:ReelDubUITests/StudioSmoke/{test}", *flags,
                    ],
                    cwd=root,
                    env={**os.environ, "TEST_RUNNER_REELDUB_SELFHOST_PROBE_URL": url,
                         "TEST_RUNNER_REELDUB_SELFHOST_PROBE_KEY": key, "TEST_RUNNER_REELDUB_UI_STORE": state},
                    check=True,
                )
        finally:
            http.shutdown()
            thread.join()
    assert requests == [("GET", "/v1/me", True), ("GET", "/v1/me", False)], requests
    print("The free probe contacted only the profile endpoint. The failed key invalidated checkout.")


if __name__ == "__main__":
    main()
