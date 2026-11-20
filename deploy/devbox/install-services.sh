#!/usr/bin/env bash
# Install user services for an already provisioned, Linux Studio host.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
bin="$HOME/.local/opt/reeldub-host"
vault="$HOME/.local/share/reeldub-vault"
cfg="$vault/open/config"
runtime="${XDG_RUNTIME_DIR:?}/reeldub-boot"
units="$HOME/.config/systemd/user"
public_host=${REELDUB_PUBLIC_HOST:?Set the public hostname}
[[ $public_host =~ ^[A-Za-z0-9.-]+$ ]]
mkdir -p -m 700 "$units" "$runtime"
mountpoint -q "$vault/open"
for name in api operator worker; do test -s "$cfg/$name.env"; done
# Use the same ingress policies as Compose. Only the listener and upstream change.
sed -e "s/\${REELDUB_PUBLIC_HOST}/$public_host/g" -e 's/listen 8080/listen 127.0.0.1:8301/g' \
    -e '/resolver 127.0.0.11/d' -e 's/hosted-api:8300/127.0.0.1:8320/g' \
    -e "s@/tmp/@$runtime/nginx/@g" "$repo/server/hosted-nginx.conf" > "$cfg/nginx.conf"
make_unit() {
    local name=$1
    cat > "$units/reeldub-$name.service"
}
make_unit vault <<EOF
[Unit]
Description=ReelDub encrypted media and credentials
Wants=reeldub-media.service reeldub-api.service reeldub-operator.service reeldub-worker.service reeldub-ingress.service
[Service]
Type=exec
UMask=0077
MemorySwapMax=0
LimitCORE=0
ExecStart=$repo/deploy/devbox/unlock-vault.sh
ExecStartPost=/usr/bin/timeout 20 /bin/sh -c 'until test -r "$cfg/ca.pem"; do sleep 0.1; done'
ExecStopPost=-/usr/bin/fusermount3 -uz $vault/open
Restart=on-failure
RestartSec=5
[Install]
WantedBy=default.target
EOF
make_unit media <<EOF
[Unit]
Description=ReelDub private encrypted media cache
After=reeldub-vault.service
BindsTo=reeldub-vault.service
PartOf=reeldub-vault.service
[Service]
ExecStartPre=/usr/bin/mountpoint -q $vault/open
ExecStart=$bin/mongod --dbpath $vault/open/mongo --bind_ip 127.0.0.1 --port 28351 --nounixsocket --auth --tlsMode requireTLS --tlsCertificateKeyFile $cfg/server.pem --tlsCAFile $cfg/ca.pem --tlsAllowConnectionsWithoutCertificates --wiredTigerCacheSizeGB 1
Restart=always
RestartSec=5
TimeoutStopSec=90
LimitCORE=0
MemorySwapMax=0
NoNewPrivileges=yes
UMask=0077
[Install]
WantedBy=default.target
EOF
for name in api operator worker; do
  port=8320; [[ $name != operator ]] || port=8300
  cmd="$HOME/.local/share/reeldub-service/bin/python -m uvicorn server.api:app --host 127.0.0.1 --port $port --no-proxy-headers --no-access-log"
  [[ $name != worker ]] || cmd="$HOME/.local/share/reeldub-service/bin/python -m server.worker"
  memory=2G; [[ $name != worker ]] || memory=48G
  make_unit "$name" <<EOF
[Unit]
Description=ReelDub $name
After=reeldub-media.service reeldub-vault.service
BindsTo=reeldub-vault.service
PartOf=reeldub-vault.service
Wants=reeldub-media.service
[Service]
WorkingDirectory=$repo
EnvironmentFile=$cfg/$name.env
Environment=REELDUB_GPU_SCHEDULER=1 WORKER_PROCESSES=2 MONGO_TLS_CA_FILE=$cfg/ca.pem TMPDIR=%t/reeldub-$name-tmp
RuntimeDirectory=reeldub-$name-tmp
RuntimeDirectoryMode=0700
MemoryMax=$memory
ExecStartPre=/usr/bin/mountpoint -q $vault/open
ExecStart=$cmd
LimitCORE=0
MemorySwapMax=0
NoNewPrivileges=yes
UMask=0077
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
EOF
done
make_unit ingress <<EOF
[Unit]
Description=ReelDub private Cloudflare origin
After=reeldub-api.service reeldub-vault.service
BindsTo=reeldub-vault.service
PartOf=reeldub-vault.service
Wants=reeldub-api.service
[Service]
ExecStartPre=/usr/bin/mkdir -p -m 0700 $runtime/nginx
ExecStartPre=/usr/bin/mountpoint -q $vault/open
MemorySwapMax=0
LimitCORE=0
ExecStart=$bin/nginx-package/usr/sbin/nginx -p $runtime/nginx -e stderr -c $cfg/nginx.conf -g "daemon off;"
Restart=always
RestartSec=5
NoNewPrivileges=yes
UMask=0077
[Install]
WantedBy=default.target
EOF
install -d -m 0700 "$bin/intake/server"
for module in __init__ intake source download media_network; do
  install -m 0600 "$repo/server/$module.py" "$bin/intake/server/$module.py"
done
make_unit intake <<EOF
[Unit]
Description=ReelDub isolated video intake
[Service]
WorkingDirectory=$repo
ExecStart=$repo/deploy/devbox/intake.sh
RuntimeDirectory=reeldub-intake-tmp
RuntimeDirectoryMode=0700
RestrictAddressFamilies=AF_INET AF_INET6
NoNewPrivileges=yes
MemoryMax=2G
MemorySwapMax=0
TasksMax=80
LimitCORE=0
UMask=0077
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
EOF
if [[ -n ${REELDUB_ORIGIN_SSH:-} ]]; then
    [[ $REELDUB_ORIGIN_SSH =~ ^[[:alnum:]_.@-]+$ ]]
    mkdir -p "$units/reeldub-vault.service.d"
    echo -e '[Unit]\nWants=reeldub-origin.service' > "$units/reeldub-vault.service.d/origin.conf"
    make_unit origin <<EOF
[Unit]
Description=Connect the private ReelDub origin to its Cloudflare gateway
After=reeldub-ingress.service reeldub-vault.service
BindsTo=reeldub-vault.service
PartOf=reeldub-vault.service
Wants=reeldub-ingress.service
[Service]
MemorySwapMax=0
LimitCORE=0
ExecStart=/usr/bin/ssh -NT -i $cfg/origin.key -o IdentitiesOnly=yes -o BatchMode=yes -o ForwardAgent=no -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$cfg/gateway-known -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ConnectTimeout=8 -R 127.0.0.1:18301:127.0.0.1:8301 $REELDUB_ORIGIN_SSH
NoNewPrivileges=yes
Restart=always
RestartSec=2
[Install]
WantedBy=default.target
EOF
fi
systemctl --user daemon-reload
