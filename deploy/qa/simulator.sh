#!/usr/bin/env bash
# Select a stable, isolated QA destination. Prints a UUID suitable for xcodebuild -destination id=...
set -euo pipefail
export REELDUB_SIM_VERSION=${REELDUB_SIM_VERSION:-27.2}
python3 - <<'PY'
import json, os, subprocess, sys
version = os.environ["REELDUB_SIM_VERSION"]
ctl = ["xcrun", "simctl"]
runtime = "com.apple.CoreSimulator.SimRuntime.iOS-" + version.replace(".", "-")
phone = "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro"
installed = json.loads(subprocess.check_output(ctl + ["list", "runtimes", "-j"]))["runtimes"]
candidates = [r for r in installed if r["identifier"] == runtime and r["isAvailable"]
              and any(d["identifier"] == phone for d in r["supportedDeviceTypes"])]
if not candidates:
    sys.exit(f"Install an iPhone 17 Pro runtime for iOS {version} in Xcode's Components settings.")
usable = [r for r in candidates if r["buildversion"] != "24C5037i"]
if not usable:
    sys.exit("This simulator build repeatedly crashes identityservicesd. Use iOS 27.2 or a fixed runtime.")
selected = next((r for r in usable if r["buildversion"] == "24B8117"), usable[0])
devices = json.loads(subprocess.check_output(ctl + ["list", "devices", "-j"]))["devices"].get(runtime, [])
name = "ReelDub QA Stable"
device = next((d for d in devices if d["name"] == name and d["isAvailable"] and d["deviceTypeIdentifier"] == phone), None)
if device:
    print(device["udid"])
else:
    print(subprocess.check_output(ctl + ["create", name, phone, selected["bundlePath"]],
                                  text=True).strip())
PY
