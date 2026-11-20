#!/bin/bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
app="$HOME/.local/opt/reeldub-host/intake"
service="$HOME/.local/share/reeldub-service"
scratch="${RUNTIME_DIRECTORY:?}"
mkdir -p -m 0700 "$scratch"
# ABI 6 supports file restrictions, TCP port rules, and process/signal scoping.
# Ubuntu 24.04 does not implement v9-only pathname socket restrictions.
python3 -c 'import ctypes,platform; assert platform.machine() in ("x86_64","aarch64") and ctypes.CDLL(None).syscall(444,0,0,1)>=6'
rules=(--best-effort --rox /usr --rox "$service" --rox "$repo/.venv" --ro "$app"
       --ro /etc/nsswitch.conf --ro /etc/hosts --ro /etc/resolv.conf --ro /etc/ssl/certs
       --ro /dev/urandom --rw /dev/null --rw "$scratch" --bind-tcp 8310 --connect-tcp 443 --connect-tcp 53 --env RES_OPTIONS=use-vc
       --env "TMPDIR=$scratch" --env "HOME=$scratch" --env "PYTHONPATH=$app" --env PYTHONDONTWRITEBYTECODE=1
       --env "PATH=$repo/.venv/bin:/usr/bin" --env REELDUB_INTAKE_PROXY_PORTS=18321,18322,18323)
for port in 18321 18322 18323; do rules+=(--bind-tcp "$port" --connect-tcp "$port"); done
cd "$app"
export PYTHONPATH="$HOME/.local/opt/reeldub-host/seccomp-package/usr/lib/python3/dist-packages"
exec /usr/bin/python3 "$repo/deploy/devbox/intake-network.py" "$HOME/.local/opt/reeldub-host/landrun" "${rules[@]}" -- "$service/bin/python" -m uvicorn server.intake:app --host 127.0.0.1 --port 8310 --no-proxy-headers --no-access-log
