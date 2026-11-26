#!/usr/bin/env bash
# Run inside the dedicated Debian guest after dependencies, staged VPN and certificates exist.
set -euo pipefail
[[ $(systemd-detect-virt --container) == lxc ]]
mountpoint -q /srv/reeldub-private
root=/srv/reeldub-private
/bin/sh /opt/reeldub-intake/deploy/proxmox-intake/require-spool.sh
updating=0
if systemctl is-active --quiet reeldub-intake-app.service; then
    [[ ${1:-} == --drained ]] || { echo 'Drain intake first, then use --drained for a live update.' >&2; exit 1; }
    shift
    [[ -z $(ss -tnH state established '( sport = :8310 )') && -z $(find "$root/spool" -mindepth 1 -print -quit) ]]
    updating=1
fi
test -s "$root/vpn/client.conf"
test -s "$root/tls/server.key"
test -s "$root/tls/ca.pem"
test -e "$root/tls/clients.map"
test -f /etc/reeldub-host-guard/ready
test -x /opt/reeldub-venv/bin/yt-dlp
test -x /opt/reeldub-venv/bin/deno
/opt/reeldub-venv/bin/python -c 'import yt_dlp_ejs'
test -e /opt/reeldub-intake/server/intake.py
test "$#" -ge 1
install -d -m 0755 /etc/reeldub
printf '%s\n' "$@" > /etc/reeldub/intake-peers
id -u reeldub-intake >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d /nonexistent reeldub-intake
id -u reeldub-egress >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d /nonexistent reeldub-egress
chmod 0700 "$root/vpn" "$root/tls"
chmod 0711 "$root"
install -d -m 0700 -o reeldub-intake -g reeldub-intake "$root/spool"
systemctl disable --now nginx ssh.service ssh.socket >/dev/null 2>&1 || true
# The packaged service's stop/reload command can flush unrelated guest tables.
systemctl mask nftables.service >/dev/null
touch /etc/.pve-ignore.resolv.conf
if [[ -f /etc/dhcp/dhclient.conf ]] && ! grep -q 'supersede domain-name-servers 1.1.1.1;' /etc/dhcp/dhclient.conf; then
    echo 'supersede domain-name-servers 1.1.1.1;' >> /etc/dhcp/dhclient.conf
fi
printf 'nameserver 1.1.1.1\noptions timeout:2 attempts:2\n' > /etc/resolv.conf
cat > /etc/reeldub/nginx.conf <<'EOF'
user www-data;
worker_processes 1;
pid /run/reeldub-ingress.pid;
events { worker_connections 96; }
http {
    access_log off;
    map $ssl_client_serial $approved_serial {
        default 0;
        include /srv/reeldub-private/tls/clients.map;
    }
    limit_conn_zone $server_addr zone=intake:1m;
    server {
        listen 8443 ssl;
        ssl_certificate /srv/reeldub-private/tls/server.pem;
        ssl_certificate_key /srv/reeldub-private/tls/server.key;
        ssl_client_certificate /srv/reeldub-private/tls/ca.pem;
        ssl_verify_client on;
        if ($approved_serial = 0) { return 403; }
        ssl_protocols TLSv1.2 TLSv1.3;
        limit_conn intake 8;
        client_max_body_size 80m;
        client_body_timeout 20s;
        location = /live { return 200 'online\n'; }
        location ~ ^/(inspect|reel)$ {
            limit_except POST { deny all; }
            proxy_pass http://127.0.0.1:8310;
            proxy_http_version 1.1;
            proxy_request_buffering off;
            proxy_buffering off;
            proxy_read_timeout 430s;
        }
        location / { return 404; }
    }
}
EOF
cat > /etc/systemd/system/reeldub-intake-firewall.service <<'EOF'
[Unit]
Description=ReelDub intake VPN-only egress
RequiresMountsFor=/srv/reeldub-private
DefaultDependencies=no
After=local-fs.target
Wants=network-pre.target
Before=network-pre.target reeldub-intake-vpn.service reeldub-intake-app.service reeldub-intake-mtls.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/usr/bin/test -f /etc/reeldub-host-guard/ready
ExecStart=/usr/bin/python3 /opt/reeldub-intake/deploy/proxmox-intake/firewall.py
ExecReload=/usr/bin/python3 /opt/reeldub-intake/deploy/proxmox-intake/firewall.py
# Rules survive a stopped unit. Removal is an explicit guest maintenance action.
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/reeldub-intake-vpn.service <<'EOF'
[Unit]
Description=ReelDub dedicated intake OpenVPN client
Requires=reeldub-intake-firewall.service
After=network-online.target reeldub-intake-firewall.service
[Service]
ExecStart=/usr/sbin/openvpn --config /srv/reeldub-private/vpn/client.conf
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
Restart=always
RestartSec=4
LimitCORE=0
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/reeldub-intake-proxy.service <<'EOF'
[Unit]
Description=Public HTTP and HTTPS egress for ReelDub media tools
Requires=reeldub-intake-firewall.service
After=reeldub-intake-firewall.service
[Service]
User=reeldub-egress
Group=reeldub-egress
WorkingDirectory=/opt/reeldub-intake
ExecStart=/usr/bin/python3 -B -m server.media_network
Environment=REELDUB_INTAKE_IPV4_ONLY=1
NoNewPrivileges=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET
RestrictNamespaces=yes
LockPersonality=yes
LimitCORE=0
MemoryMax=96M
TasksMax=80
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/reeldub-intake-app.service <<'EOF'
[Unit]
Description=Credential-free ReelDub intake
Requires=reeldub-intake-firewall.service reeldub-intake-proxy.service
After=reeldub-intake-firewall.service reeldub-intake-proxy.service
[Service]
User=reeldub-intake
Group=reeldub-intake
WorkingDirectory=/opt/reeldub-intake
ExecStart=/opt/reeldub-venv/bin/uvicorn server.intake:app --host 127.0.0.1 --port 8310 --no-access-log --no-proxy-headers
Environment=PATH=/opt/reeldub-venv/bin:/usr/bin PYTHONDONTWRITEBYTECODE=1 HOME=/run/reeldub-intake TMPDIR=/srv/reeldub-private/spool
Environment=REELDUB_INTAKE_FIXED_PROXY=1
ExecStartPre=/bin/sh /opt/reeldub-intake/deploy/proxmox-intake/require-spool.sh
ExecStartPre=/usr/bin/find /srv/reeldub-private/spool -mindepth 1 -delete
RuntimeDirectory=reeldub-intake
RuntimeDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6
RestrictNamespaces=yes
LockPersonality=yes
RestrictSUIDSGID=yes
SystemCallArchitectures=native
LimitCORE=0
TasksMax=64
MemoryMax=720M
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/reeldub-intake-mtls.service <<'EOF'
[Unit]
Description=Private ReelDub intake mutual TLS listener
Requires=reeldub-intake-firewall.service
Wants=reeldub-intake-app.service
After=reeldub-intake-app.service reeldub-intake-firewall.service
[Service]
ExecStartPre=/usr/sbin/nginx -t -q -c /etc/reeldub/nginx.conf
ExecStart=/usr/sbin/nginx -g 'daemon off;' -c /etc/reeldub/nginx.conf
ExecReload=/usr/sbin/nginx -s reload -c /etc/reeldub/nginx.conf
Restart=always
RestartSec=3
LimitCORE=0
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now reeldub-intake-firewall.service
systemctl enable --now reeldub-intake-vpn.service reeldub-intake-proxy.service reeldub-intake-app.service reeldub-intake-mtls.service
if (( updating )); then
    systemctl restart reeldub-intake-proxy
    systemctl restart reeldub-intake-app
    systemctl reload reeldub-intake-mtls
fi
systemctl reload reeldub-intake-firewall.service
