#!/usr/bin/env bash
# Select a stable, isolated QA destination. Prints a UUID suitable for xcodebuild -destination id=...
set -euo pipefail
export REELDUB_SIM_VERSION=${REELDUB_SIM_VERSION:-27.2}
python3 - <<'PY'
import json, os, subprocess, sys
version = os.environ["REELDUB_SIM_VERSION"]
ctl = ["xcrun", "simctl"]
runtime = "com.apple.CoreSimulator.SimRuntime.iOS-" + version.replace(".", "-")
installed = json.loads(subprocess.check_output(ctl + ["list", "runtimes", "-j"]))["runtimes"]
if not any(r["identifier"] == runtime and r["isAvailable"] for r in installed):
    sys.exit(f"Install the selected runtime with xcodebuild -downloadPlatform iOS -buildVersion {version}")
devices = json.loads(subprocess.check_output(ctl + ["list", "devices", "-j"]))["devices"][runtime]
name = "ReelDub QA Stable"
device = next((d for d in devices if d["name"] == name and d["isAvailable"]), None)
if device:
    print(device["udid"])
else:
    print(subprocess.check_output(ctl + ["create", name, "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro", runtime],
                                  text=True).strip())
PY
