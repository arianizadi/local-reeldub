#!/bin/bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
app="$HOME/.local/opt/reeldub-host/intake"
service="$HOME/.local/share/reeldub-service"
scratch="${RUNTIME_DIRECTORY:?}"
install -d -m 0700 "$scratch"
# ABI 6 supports file restrictions, TCP port rules, and process/signal scoping.
# Ubuntu 24.04 does not implement v9-only pathname socket restrictions.
python3 -c 'import ctypes,platform; assert platform.machine() in ("x86_64","aarch64") and ctypes.CDLL(None).syscall(444,0,0,1)>=6'
rules=(--best-effort --rox /usr --rox "$service" --rox "$repo/.venv" --ro "$app"
       --ro /etc/nsswitch.conf --ro /etc/hosts --ro /etc/resolv.conf --ro /etc/ssl/certs
       --ro /dev/urandom --rw /dev/null --rw "$scratch"
       --env "TMPDIR=$scratch" --env "HOME=$scratch" --env "PYTHONPATH=$app" --env PYTHONDONTWRITEBYTECODE=1
       --env "PATH=$repo/.venv/bin:/usr/bin")
if [[ ${1:-} == --proxy ]]; then
    rules+=(--bind-tcp 18322 --connect-tcp 80 --connect-tcp 443 --connect-tcp 53 --env RES_OPTIONS=use-vc)
    run=("$service/bin/python" -m server.media_network)
else
    rules+=(--bind-tcp 8310 --connect-tcp 18322 --env REELDUB_INTAKE_FIXED_PROXY=1)
    run=("$service/bin/python" -m uvicorn server.intake:app --host 127.0.0.1 --port 8310 --no-proxy-headers --no-access-log)
fi
cd "$app"
export PYTHONPATH="$HOME/.local/opt/reeldub-host/seccomp-package/usr/lib/python3/dist-packages"
exec /usr/bin/python3 "$repo/deploy/devbox/intake-network.py" "$HOME/.local/opt/reeldub-host/landrun" "${rules[@]}" -- "${run[@]}"
