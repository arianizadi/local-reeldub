"""Run from the repo root with a signed-out simulator:

    python3 deploy/qa/test_drag_selection.py "$(deploy/qa/simulator.sh)"

Optional xcodebuild flags, such as -resultBundlePath, may follow the simulator UUID.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import uuid


def fixture(key):
    jobs = [
        {
            "id": f"drag-{key}-{index:02d}",
            "title": f"Drag {key} {index:02d}",
            "url": "https://example.invalid/synthetic",
            "state": "failed",
            "stage": "Failed",
            "error": "Synthetic test row",
            "created": f"2026-01-01T12:00:{59 - index:02d}Z",
            "reported": False,
        }
        for index in range(12)
    ]
    deleted = []

    class Library(BaseHTTPRequestHandler):
        def reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authenticated(self):
            if self.headers.get("Authorization") == f"Bearer {key}":
                return True
            self.reply(401, {"detail": "Unknown fixture key"})
            return False

        def do_GET(self):
            if not self.authenticated():
                return
            if self.path == "/v1/me":
                self.reply(200, {"library_id": key, "output_languages": ["en"], "quality_languages": {"best": ["en"], "fast": ["en"]}})
            elif self.path == "/v1/jobs":
                self.reply(200, jobs)
            else:
                self.reply(404, {})

        def do_DELETE(self):
            if not self.authenticated():
                return
            if self.path == "/v1/session":
                self.reply(200, {})
                return
            for job in jobs:
                if self.path == "/v1/jobs/" + job["id"]:
                    jobs.remove(job)
                    deleted.append(job["id"])
                    self.reply(200, {})
                    return
            self.reply(404, {})

    return HTTPServer(("127.0.0.1", 0), Library), deleted


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    simulator = str(uuid.UUID(sys.argv[1])).upper()
    root = Path(__file__).resolve().parents[2]
    key = secrets.token_hex(4)
    server, deleted = fixture(key)
    with server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                [
                    "xcodebuild", "test", "-project", "ios/ReelDub.xcodeproj",
                    "-scheme", "ReelDub", "-configuration", "Debug",
                    "-destination", f"platform=iOS Simulator,id={simulator}",
                    "-parallel-testing-enabled", "NO",
                    "-only-testing:ReelDubUITests/StudioSmoke/testRangeSelectionAndConfirmedDeletion",
                    *sys.argv[2:],
                ],
                cwd=root,
                env={
                    **os.environ,
                    "TEST_RUNNER_REELDUB_DRAG_FIXTURE_PORT": str(server.server_port),
                    "TEST_RUNNER_REELDUB_DRAG_FIXTURE_KEY": key,
                },
                check=True,
            )
        finally:
            server.shutdown()
            thread.join()

    expected = [f"drag-{key}-00", f"drag-{key}-03"]
    if sorted(deleted) != expected:
        sys.exit(f"Expected exactly two fixture deletions {expected}, received {deleted}.")
    print("Verified both selected fixture rows were deleted and every other row was retained.")


if __name__ == "__main__":
    main()
