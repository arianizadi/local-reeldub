#!/usr/bin/env bash
# On the devbox: read the controller's Ed25519 SSH host public key from an authenticated management channel.
set -euo pipefail
address=$(python3 -c 'import ipaddress,sys; a=ipaddress.IPv4Address(sys.argv[1]); assert a.is_private; print(a)' "${1:?control guest LAN IPv4}")
vault="$HOME/.local/share/reeldub-vault/open"
mountpoint -q "$vault"
umask 077
private="$vault/config/operator-forward"
install -d -m 0700 "$private"
if [[ ! -e $private/id_ed25519 ]]; then
    ssh-keygen -q -t ed25519 -N '' -C reeldub-operator-forward -f "$private/id_ed25519"
fi
read -r algorithm public _ || true
[[ $algorithm == ssh-ed25519 && $public =~ ^[A-Za-z0-9+/=]+$ ]]
printf '%s %s %s\n' "$address" "$algorithm" "$public" > "$private/known_hosts.new"
ssh-keygen -lf "$private/known_hosts.new" >/dev/null
mv "$private/known_hosts.new" "$private/known_hosts"
units="$HOME/.config/systemd/user"
install -d -m 0700 "$units"
cat > "$units/reeldub-operator-forward@.service" <<EOF
[Unit]
Description=ReelDub loopback operator relay on port %i
After=reeldub-vault.service
BindsTo=reeldub-vault.service
PartOf=reeldub-vault.service
[Service]
ExecStartPre=/usr/bin/mountpoint -q %h/.local/share/reeldub-vault/open
ExecStart=/usr/bin/ssh -F /dev/null -NT -o BatchMode=yes -o ExitOnForwardFailure=yes -o IdentitiesOnly=yes -o IdentityAgent=none -o StrictHostKeyChecking=yes -o HostKeyAlgorithms=ssh-ed25519 -o GlobalKnownHostsFile=/dev/null -o UserKnownHostsFile=$private/known_hosts -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ConnectTimeout=6 -i $private/id_ed25519 -L 127.0.0.1:%i:127.0.0.1:8300 reeldub-op-tunnel@$address
Restart=always
RestartSec=3
NoNewPrivileges=yes
LimitCORE=0
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
