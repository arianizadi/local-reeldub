#!/usr/bin/env bash
# Run as the Linux Studio model owner. Start the printed instances after draining any direct workers.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
cfg=${REELDUB_WORKER_CONFIG:?Private directory containing ca.pem, client.pem, client.key and token}
url=${REELDUB_FLEET_URL:?Private HTTPS coordinator URL}
node=${REELDUB_FLEET_NODE:?Enrolled node ID}
slots=${REELDUB_GPU_PIPELINE_SLOTS:-1}
service_python=${REELDUB_SERVICE_PYTHON:?Python with server requirements installed}
gpu=${REELDUB_DEVICE_ID:-0}
[[ $url =~ ^https://[A-Za-z0-9.:_-]+$ && $node =~ ^[a-z0-9][a-z0-9-]{0,48}$ && $slots =~ ^[12]$ && $gpu =~ ^[a-zA-Z0-9-]+$ ]]
test -f "$repo/.qwen-venv/ready"
for f in ca.pem client.pem client.key token; do test -r "$cfg/$f"; done
units="$HOME/.config/systemd/user"
mkdir -p "$units"
name="reeldub-render-$node"
landrun=${REELDUB_LANDRUN:-$HOME/.local/opt/reeldub-host/landrun}
test -x "$landrun"
entry="$repo/deploy/fleet/isolate.sh worker"
cat > "$units/$name@.service" <<EOF
[Unit]
Description=ReelDub GPU $node slot %i
After=network-online.target
[Service]
WorkingDirectory=$repo
Environment=REELDUB_SERVICE_PYTHON=$service_python REELDUB_LANDRUN=$landrun
Environment=REELDUB_FLEET_SLOT=%i REELDUB_FLEET_NODE=$node REELDUB_FLEET_URL=$url
Environment=REELDUB_FLEET_CA=$cfg/ca.pem REELDUB_FLEET_CERT=$cfg/client.pem REELDUB_FLEET_KEY=$cfg/client.key REELDUB_FLEET_TOKEN_FILE=$cfg/token
Environment=REELDUB_DEVICE_ID=$gpu REELDUB_LOCAL_GPU_METRICS=1 CUDA_VISIBLE_DEVICES=$gpu REELDUB_GPU_LOCK_DIR=%h/.cache/reeldub/gpu$gpu REELDUB_GPU_SCHEDULER=1 REELDUB_GPU_PIPELINE_SLOTS=$slots
Environment=TMPDIR=%t/$name-%i
RuntimeDirectory=$name-%i
RuntimeDirectoryMode=0700
NoNewPrivileges=yes
LimitCORE=0
MemorySwapMax=0
UMask=0077
ExecStart=$entry
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
for ((s=0;s<slots;s++)); do echo "systemctl --user enable --now $name@$s.service"; done
