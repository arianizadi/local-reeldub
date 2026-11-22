#!/usr/bin/env bash
# Provision the control guest without restarting active units. See README for cutover.
set -euo pipefail
umask 077
action=${1:-prepare}
repo=${REELDUB_CONTROL_DIR:-/opt/reeldub}
private=${REELDUB_PRIVATE_DIR:-/srv/reeldub-private}
cfg="$private/config"
scratch="$private/service-tmp"
units=(media api operator fleet ingress fleet-proxy origin)
mountpoint -q "$private"
[[ $repo =~ ^/[A-Za-z0-9/._-]+$ && $private =~ ^/[A-Za-z0-9/._-]+$ ]]
[[ $(id -u) = 0 ]]
if [[ $action == media-ownership ]]; then
    if systemctl is-active --quiet reeldub-media.service; then
        echo 'Stop the media service cleanly before migrating database ownership.' >&2
        exit 1
    fi
    cmp -s /etc/reeldub-control-volume-id "$private/.control-volume-id"
    chown -R --no-dereference reeldub-media:reeldub-media "$private/mongo"
    chmod 0700 "$private/mongo"
    exit
fi
if [[ $action == enable ]]; then
    cmp -s /etc/reeldub-control-volume-id "$private/.control-volume-id"
    [[ $(stat -c %U "$private/mongo") == reeldub-media ]]
    for name in "${units[@]}"; do systemctl enable "reeldub-$name.service"; done
    exit
fi
[[ $action == prepare ]] || { echo 'Expected prepare, media-ownership or enable.' >&2; exit 2; }
addr=${REELDUB_FLEET_IP:?private fleet IPv4}
address=$(python3 -c 'import ipaddress,sys; a=ipaddress.IPv4Address(sys.argv[1]); assert a.is_private; print(a)' "$addr")
host=${REELDUB_PUBLIC_HOST:?public Cloudflare hostname}
origin=${REELDUB_ORIGIN_SSH:?existing restricted SSH reverse-forward account}
[[ $host =~ ^[A-Za-z0-9.-]+$ && $origin =~ ^[A-Za-z0-9.@_-]+$ ]]
findmnt -rn -M "$private" >/dev/null
if [[ -n ${REELDUB_EXPECTED_PRIVATE_SOURCE:-} ]]; then
    [[ $(findmnt -nr -o SOURCE -M "$private") == "$REELDUB_EXPECTED_PRIVATE_SOURCE" ]]
fi
required=(api.env operator.env fleet.env intake.env api.dsn ca.pem server.pem fleet-registry.json
    fleet-ca.pem fleet-tls.pem fleet-tls.key origin.key gateway-known
    intake/ca.pem intake/client.pem intake/client.key)
for file in "${required[@]}"; do
    [[ -f $cfg/$file && ! -L $cfg/$file && -s $cfg/$file ]] || { echo "Missing physical configuration: $file" >&2; exit 1; }
done
for path in "$repo" "$repo/venv/bin/python" "$repo/mongod"; do test -x "$path"; done
openssl verify -purpose sslclient -CAfile "$cfg/intake/ca.pem" "$cfg/intake/client.pem" >/dev/null
openssl verify -purpose sslserver -verify_ip "$address" -CAfile "$cfg/fleet-ca.pem" "$cfg/fleet-tls.pem" >/dev/null
if [[ ! -s /etc/reeldub-control-volume-id ]]; then
    if [[ ! -s $private/.control-volume-id ]]; then cat /proc/sys/kernel/random/uuid > "$private/.control-volume-id"; fi
    install -m 0444 "$private/.control-volume-id" /etc/reeldub-control-volume-id
fi
chmod 0444 "$private/.control-volume-id"
cmp -s /etc/reeldub-control-volume-id "$private/.control-volume-id"
getent group reeldub >/dev/null || groupadd -g 1915 reeldub
[[ $(getent group reeldub | cut -d: -f3) == 1915 ]]
for name in "${units[@]}"; do
    account="reeldub-$name"
    getent group "$account" >/dev/null || groupadd --system "$account"
    id "$account" >/dev/null 2>&1 || useradd --system --no-create-home -g "$account" -d /nonexistent -s /usr/sbin/nologin "$account"
    [[ $(id -gn "$account") == "$account" ]]
done
for name in api operator fleet; do usermod -aG reeldub "reeldub-$name"; done
for name in media ingress fleet-proxy origin; do
    if id -G "reeldub-$name" | tr ' ' '\n' | grep -qx 1915; then echo "Unexpected metadata group on $name" >&2; exit 1; fi
done
for name in "${units[@]}"; do
    case $name in
        api|operator|fleet) executable="$repo/venv/bin/python" ;;
        media) executable="$repo/mongod" ;;
        ingress|fleet-proxy) executable=/usr/sbin/nginx ;;
        origin) executable=/usr/bin/ssh ;;
    esac
    runuser -u "reeldub-$name" -- test -x "$repo"
    runuser -u "reeldub-$name" -- test -x "$executable"
done
chown root:root "$private" "$cfg"
chmod 0711 "$private" "$cfg"
install -d -m 0711 -o root -g root "$scratch"
for name in "${units[@]}"; do
    install -d -m 0700 -o "reeldub-$name" -g "reeldub-$name" "$scratch/$name"
done
if [[ ! -d $private/mongo ]]; then install -d -m 0700 -o reeldub-media -g reeldub-media "$private/mongo"; fi
chown root:root "$cfg"/{api,operator,fleet,intake}.env
chmod 0600 "$cfg"/{api,operator,fleet,intake}.env
chown root:reeldub "$cfg"/{api.dsn,fleet-registry.json}
chmod 0640 "$cfg"/{api.dsn,fleet-registry.json}
chown root:reeldub "$cfg/intake" "$cfg/intake"/{ca.pem,client.key,client.pem}
chmod 0750 "$cfg/intake"
chmod 0640 "$cfg/intake"/{ca.pem,client.key,client.pem}
chown root:root "$cfg/ca.pem"
chmod 0644 "$cfg/ca.pem"
chown root:reeldub-media "$cfg/server.pem"
chmod 0640 "$cfg/server.pem"
chown root:reeldub-fleet-proxy "$cfg"/{fleet-ca.pem,fleet-tls.key,fleet-tls.pem}
chmod 0640 "$cfg"/{fleet-ca.pem,fleet-tls.key,fleet-tls.pem}
chown reeldub-origin:reeldub-origin "$cfg/origin.key"
chmod 0600 "$cfg/origin.key"
chown root:reeldub-origin "$cfg/gateway-known"
chmod 0640 "$cfg/gateway-known"
sed -e "s/\${REELDUB_PUBLIC_HOST}/$host/g" -e 's/listen 8080/listen 127.0.0.1:8301/g' \
    -e '/resolver 127.0.0.11/d' -e 's/hosted-api:8300/127.0.0.1:8320/g' -e "s@/tmp/@$scratch/ingress/@g" \
    "$repo/server/hosted-nginx.conf" > "$cfg/ingress-isolated.conf"
sed -e "s@FLEET_TMP@$scratch/fleet-proxy@g" -e "s/FLEET_ADDR/$address/g" -e "s@FLEET_CONFIG@$cfg@g" \
    "$repo/deploy/fleet/worker-nginx.conf" > "$cfg/fleet-proxy-isolated.conf"
chown root:reeldub-ingress "$cfg/ingress-isolated.conf"
chown root:reeldub-fleet-proxy "$cfg/fleet-proxy-isolated.conf"
chmod 0640 "$cfg"/{ingress-isolated,fleet-proxy-isolated}.conf
# File ownership is the primary boundary. Add mount namespaces only when this guest actually supports them.
options=(PrivateTmp=yes ProtectHome=yes ProtectSystem=strict ProtectProc=invisible PrivateDevices=yes
    RestrictNamespaces=yes ProtectKernelTunables=yes RestrictSUIDSGID=yes LockPersonality=yes)
props=()
for opt in "${options[@]}"; do props+=(-p "$opt"); done
if ! systemd-run --quiet --wait --pipe --collect -p User=nobody "${props[@]}" /bin/true >/dev/null 2>&1; then
    echo 'Guest mount namespace profile unavailable; using distinct UIDs, mode bits and no capabilities.' >&2
    options=()
fi
unit() {
    local name=$1 command=$2 extra=$3 dependencies=$4 writable="$scratch/$1"
    [[ $name != media ]] || writable+=" $private/mongo"
    {
        cat <<EOF
[Unit]
Description=ReelDub $name
After=network-online.target $dependencies
Wants=network-online.target $dependencies
RequiresMountsFor=$private
[Service]
User=reeldub-$name
Group=reeldub-$name
WorkingDirectory=$repo
ExecStartPre=/usr/bin/mountpoint -q $private
ExecStartPre=/usr/bin/cmp -s /etc/reeldub-control-volume-id $private/.control-volume-id
Environment=TMPDIR=$scratch/$name PYTHONDONTWRITEBYTECODE=1
$extra
ExecStart=$command
UMask=0077
NoNewPrivileges=yes
CapabilityBoundingSet=
LimitCORE=0
Restart=on-failure
RestartSec=4
EOF
        if (( ${#options[@]} )); then
            printf '%s\n' "${options[@]}"
            printf 'ReadWritePaths=%s\n' "$writable"
            [[ $name == media ]] || printf 'InaccessiblePaths=-%s/mongo\n' "$private"
        fi
        printf '[Install]\nWantedBy=multi-user.target\n'
    } > "/etc/systemd/system/reeldub-$name.service"
    chmod 0644 "/etc/systemd/system/reeldub-$name.service"
}
unit media "$repo/mongod --dbpath $private/mongo --bind_ip 127.0.0.1 --port 28351 --nounixsocket --auth --tlsMode requireTLS --tlsCertificateKeyFile $cfg/server.pem --tlsCAFile $cfg/ca.pem --tlsAllowConnectionsWithoutCertificates --wiredTigerCacheSizeGB 0.45" "" ""
common="Environment=MONGO_TLS_CA_FILE=$cfg/ca.pem REELDUB_FLEET_REGISTRY=$cfg/fleet-registry.json
EnvironmentFile=$cfg/intake.env"
unit api "$repo/venv/bin/python -m uvicorn server.api:app --host 127.0.0.1 --port 8320 --no-access-log --no-proxy-headers" "EnvironmentFile=$cfg/api.env
$common" "reeldub-media.service"
unit operator "$repo/venv/bin/python -m uvicorn server.api:app --host 127.0.0.1 --port 8300 --no-access-log --no-proxy-headers" "EnvironmentFile=$cfg/operator.env
$common" "reeldub-media.service"
unit fleet "$repo/venv/bin/python -m uvicorn server.fleet:app --host 127.0.0.1 --port 8344 --no-access-log --no-proxy-headers" "EnvironmentFile=$cfg/fleet.env
$common" "reeldub-media.service"
unit fleet-proxy "/usr/sbin/nginx -p $scratch/fleet-proxy -e stderr -c $cfg/fleet-proxy-isolated.conf -g 'daemon off;'" "" "reeldub-fleet.service"
unit ingress "/usr/sbin/nginx -p $scratch/ingress -e stderr -c $cfg/ingress-isolated.conf -g 'daemon off;'" "" "reeldub-api.service"
unit origin "/usr/bin/ssh -F /dev/null -NT -i $cfg/origin.key -o IdentityAgent=none -o IdentitiesOnly=yes -o BatchMode=yes -o ForwardAgent=no -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null -o UserKnownHostsFile=$cfg/gateway-known -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ConnectTimeout=8 -R 127.0.0.1:18301:127.0.0.1:8301 $origin" "" "reeldub-ingress.service"
systemctl daemon-reload
