#!/usr/bin/env bash
# Run on the Proxmox host for the dedicated, unprivileged intake guest.
set -euo pipefail
guest=${1:?guest ID}
pool=${2:?ZFS pool}
[[ $guest =~ ^[0-9]+$ && $pool =~ ^[A-Za-z0-9_-]+$ ]]
pct config "$guest" | grep -qx 'unprivileged: 1'
dataset="$pool/reeldub-intake-private"
mount=/var/lib/reeldub-private/intake
issuer=/var/lib/reeldub-private/intake-issuer
spool=/var/lib/reeldub-private/intake-spool
key=/etc/reeldub-keys/intake.raw
umask 077
install -d -m 0700 /etc/reeldub-keys /var/lib/reeldub-private
if ! zfs list -H "$dataset" >/dev/null 2>&1; then
    if [[ ! -e $key ]]; then (set -C; head -c 32 /dev/urandom > "$key"); fi
    [[ $(stat -c %s "$key") == 32 ]]
    zfs create -o encryption=aes-256-gcm -o keyformat=raw -o keylocation="file://$key" \
        -o mountpoint="$mount" -o quota=2304M -o devices=off -o setuid=off -o exec=off "$dataset"
fi
[[ $(zfs get -H -o value encryption "$dataset") == aes-256-gcm ]]
[[ $(zfs get -H -o value mountpoint "$dataset") == "$mount" ]]
[[ $(zfs get -H -o value keylocation "$dataset") == "file://$key" ]]
zfs set quota=2304M "$dataset"
if ! zfs list -H "$dataset/issuer" >/dev/null 2>&1; then
    zfs create -o mountpoint="$issuer" -o quota=32M "$dataset/issuer"
fi
[[ $(zfs get -H -o value encryptionroot "$dataset/issuer") == "$dataset" ]]
[[ $(zfs get -H -o value mountpoint "$dataset/issuer") == "$issuer" ]]
zfs set reservation=32M "$dataset/issuer"
if ! zfs list -H "$dataset/spool" >/dev/null 2>&1; then
    zfs create -o mountpoint="$spool" -o quota=2G -o refquota=2G "$dataset/spool"
fi
[[ $(zfs get -H -o value encryptionroot "$dataset/spool") == "$dataset" ]]
[[ $(zfs get -H -o value mountpoint "$dataset/spool") == "$spool" ]]
chmod 0700 "$issuer"
cat > /etc/systemd/system/reeldub-intake-storage.service <<EOF
[Unit]
Description=Unlock the encrypted ReelDub intake configuration
After=zfs-import.target
Before=pve-guests.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -ec 'if [ "\$(zfs get -H -o value keystatus $dataset)" != available ]; then zfs load-key $dataset; fi; if ! mountpoint -q $mount; then zfs mount $dataset; fi'
ExecStartPost=/bin/sh -ec 'if ! mountpoint -q $issuer; then zfs mount $dataset/issuer; fi'
ExecStartPost=/bin/sh -ec 'if ! mountpoint -q $spool; then zfs mount $dataset/spool; fi'
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now reeldub-intake-storage.service
mountpoint -q "$mount"
hostpid=$(lxc-info -n "$guest" -pH)
mapped_id() {
    awk -v n="$2" '$1 <= n && n < $1+$3 {print $2+n-$1; exit}' "/proc/$hostpid/${1}_map"
}
mapped=$(mapped_id uid 0)
mapped_group=$(mapped_id gid 0)
[[ $mapped =~ ^[1-9][0-9]+$ && $mapped_group =~ ^[1-9][0-9]+$ ]]
chown "$mapped:$mapped_group" "$mount"
chmod 0711 "$mount"
binding="$mount,mp=/srv/reeldub-private"
if pct config "$guest" | grep -q '^mp0:'; then
    pct config "$guest" | grep -Fqx "mp0: $binding"
else
    pct set "$guest" --mp0 "$binding"
fi
pct exec "$guest" -- sh -ec 'id -u reeldub-intake >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d /nonexistent reeldub-intake'
guest_uid=$(pct exec "$guest" -- id -u reeldub-intake)
guest_gid=$(pct exec "$guest" -- id -g reeldub-intake)
owner=$(mapped_id uid "$guest_uid")
group=$(mapped_id gid "$guest_gid")
[[ $owner =~ ^[1-9][0-9]+$ && $group =~ ^[1-9][0-9]+$ ]]
chown "$owner:$group" "$spool"
chmod 0700 "$spool"
binding="$spool,mp=/srv/reeldub-private/spool"
if pct config "$guest" | grep -q '^mp2:'; then
    pct config "$guest" | grep -Fqx "mp2: $binding"
else
    # Refuse to cover a file that is actively being streamed.
    [[ ! -d $mount/spool || -z $(find "$mount/spool" -mindepth 1 -print -quit) ]]
    install -d -m 0700 -o "$owner" -g "$group" "$mount/spool"
    pct set "$guest" --mp2 "$binding"
fi
zfs set refquota=128M refreservation=32M "$dataset"
