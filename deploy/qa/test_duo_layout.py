"""Run ReelDub's live fold/rotation UI test on a disposable iPhone Duo.

Install Apple's Duo-compatible iOS 27.2 runtime once:
    xcodebuild -downloadPlatform iOS -buildVersion 24B91016s -architectureVariant arm64
Run from the repository:
    python3 deploy/qa/test_duo_layout.py --result /tmp/ReelDub-Duo.xcresult

Install AXe 2.1.1+ (https://github.com/cameroncooke/AXe) to drive the native hinge.
Set --axe if its executable is outside PATH. Apple account
sign-in, payment and the contents of the operator's existing simulators are outside
this layout test. Device deletion on exit only touches the simulator created here.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from test_drag_selection import fixture

SIMCTL = ["xcrun", "simctl"]
DUO = "com.apple.CoreSimulator.SimDeviceType.iPhone-Duo"
REPORTS = Path.home() / "Library/Logs/DiagnosticReports"


def run_monitored(command, device, known_reports, *, env=None):
    process = subprocess.Popen(command, start_new_session=True, env=env)
    try:
        while process.poll() is None:
            for report in REPORTS.glob("identityservicesd*.ips"):
                if report in known_reports:
                    continue
                try:
                    if device in report.read_text(errors="replace"):
                        raise RuntimeError(f"Simulator identity service crashed ({report.name}); stopping this run.")
                except FileNotFoundError:
                    continue
            time.sleep(1)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def hinge_fixture(axe, device, key):
    # AXe can briefly hold its lock after a command exits. The powered display
    # may also be changing at boot. Repeating a relative rotation would be unsafe.
    lock = threading.Lock()

    def command(*args):
        with lock:
            for attempt in range(5):
                result = subprocess.run([axe, *args, "--udid", device], capture_output=True, timeout=18)
                if not result.returncode:
                    return result.stdout
                busy = b"Another AXe command is using the simulator" in result.stderr
                transitioning = b"inconsistent screen layout" in result.stderr and args[0] != "rotate"
                if not (busy or transitioning) or attempt == 4:
                    raise RuntimeError(result.stderr.decode(errors="replace").strip())
                time.sleep(0.2 * (attempt + 1))

    class Hinge(BaseHTTPRequestHandler):
        def do_GET(self):
            actions = ("closed", "tabletop", "open", "left", "right", "screenshot")
            action = next((a for a in actions if self.path == f"/{key}/{a}"), None)
            if not action:
                self.send_error(404)
                return
            try:
                if action == "screenshot":
                    # XCUIScreen can return the inactive (black) display on Duo.
                    with tempfile.TemporaryDirectory() as folder:
                        path = Path(folder) / "active.png"
                        command("screenshot", "--output", str(path))
                        content, mime = path.read_bytes(), "image/png"
                else:
                    if action in ("left", "right"):
                        command("rotate", action)
                    else:
                        command("hinge", "set", action)
                    screen = json.loads(command("--output", "json", "displays"))["data"]["result"]["activeScreen"]
                    print(f"{action}: {screen['orientation']} {screen['width']}x{screen['height']}", flush=True)
                    content, mime = json.dumps(screen).encode(), "application/json"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                print(f"Duo simulator action {action} failed: {error}", flush=True)
                self.send_error(503, "Simulator control unavailable")

        def log_message(self, format, *args):
            pass  # The URL contains a random key for this test run.

    return ThreadingHTTPServer(("127.0.0.1", 0), Hinge)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--result", type=Path, required=True, help="New .xcresult output path")
    parser.add_argument("--axe", default=shutil.which("axe"), help="Path to the AXe simulator CLI")
    parser.add_argument("--runtime-build", default="24B91016s", help="Installed Duo-compatible simulator build")
    args = parser.parse_args()
    if not args.axe or not os.access(args.axe, os.X_OK):
        parser.error("Install AXe 2.1.1+ and pass --axe /path/to/axe.")
    result = args.result.resolve()
    if result.exists():
        parser.error("Choose a new result bundle path.")
    # This beta repeatedly crashes identityservicesd even with --disabledJob on this host.
    if args.runtime_build == "24C5037i":
        parser.error("Use a stable Duo-compatible runtime. The 24C5037i beta has a known simulator crash.")
    runtimes = json.loads(subprocess.check_output([*SIMCTL, "runtime", "list", "-j"]))
    runtime = next((r for r in runtimes.values() if r["build"] == args.runtime_build and r["state"] == "Ready"
                    and r["platformIdentifier"] == "com.apple.platform.iphonesimulator"), None)
    if not runtime:
        parser.error(f"iOS simulator build {args.runtime_build} is unavailable. See the installation command above.")
    # The general and Duo-specific iOS 27.2 runtime images can coexist. Ask for the exact image.
    device = subprocess.check_output([*SIMCTL, "create", "ReelDub Duo Layout QA", DUO,
                                      runtime["runtimeBundlePath"]], text=True).strip()
    try:
        before = set(REPORTS.glob("identityservicesd*.ips"))
        run_monitored([*SIMCTL, "bootstatus", device, "-b"], device, before)
        actual = subprocess.check_output([*SIMCTL, "getenv", device, "SIMULATOR_RUNTIME_BUILD_VERSION"], text=True).strip()
        if actual != args.runtime_build:
            raise RuntimeError(f"Requested {args.runtime_build}, but CoreSimulator booted {actual}.")
        print(f"Testing iPhone Duo simulator {device} on build {actual}", flush=True)
        root = Path(__file__).resolve().parents[2]
        key = secrets.token_hex(4)
        server, deleted = fixture(key)
        with server, hinge_fixture(args.axe, device, key) as hinge, tempfile.TemporaryDirectory(prefix="ReelDub-Duo-") as build:
            threads = [threading.Thread(target=listener.serve_forever, daemon=True) for listener in (server, hinge)]
            for thread in threads:
                thread.start()
            try:
                run_monitored(["xcodebuild", "test", "-project", str(root / "ios/ReelDub.xcodeproj"),
                               "-scheme", "ReelDub", "-configuration", "Debug", "-destination", f"id={device}",
                               "-derivedDataPath", build, "-jobs", "2", "-parallel-testing-enabled", "NO",
                               "-only-testing:ReelDubTests/StoreCatalogTests/testPrepareSelfHostUITestStore",
                               "-only-testing:ReelDubUITests/DuoLayouts/testFormAndHelpSurviveFoldingAndRotation",
                               "-only-testing:ReelDubUITests/StudioSmoke/testLibrarySelectionSurvivesUnfolding",
                               "-resultBundlePath", str(result)], device, before, env={
                                   **os.environ, "TEST_RUNNER_REELDUB_UI_STORE": "owned",
                                   "TEST_RUNNER_REELDUB_DRAG_FIXTURE_PORT": str(server.server_port),
                                   "TEST_RUNNER_REELDUB_DRAG_FIXTURE_KEY": key,
                                   "TEST_RUNNER_REELDUB_DUO_HINGE_URL": f"http://127.0.0.1:{hinge.server_port}/{key}",
                               })
            finally:
                server.shutdown()
                hinge.shutdown()
                for thread in threads:
                    thread.join()
        if deleted:
            raise RuntimeError("Duo library selection unexpectedly deleted fixture rows.")
        summary = json.loads(subprocess.check_output(["xcrun", "xcresulttool", "get", "test-results",
                                                       "summary", "--path", str(result)]))
        if summary.get("passedTests") != 3 or summary.get("skippedTests", 0) or summary.get("failedTests", 0):
            raise RuntimeError(f"The Duo fold test did not pass. Inspect {result}.")
        print(f"Verified fold, rotation, and state restoration. Result bundle: {result}")
    finally:
        subprocess.run([*SIMCTL, "shutdown", device], check=False)
        subprocess.run([*SIMCTL, "delete", device], check=True)


if __name__ == "__main__":
    main()
