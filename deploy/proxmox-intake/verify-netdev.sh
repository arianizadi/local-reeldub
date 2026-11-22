#!/usr/bin/env bash
# Probe this kernel in an isolated network namespace, before applying the real per-device guard.
set -euo pipefail
exec unshare --net /bin/bash -se <<'NET'
set -euo pipefail
nft 'add table netdev reeldub_probe'
nft 'add chain netdev reeldub_probe bound { type filter hook ingress device "rdcheck0" priority 0; }'
nft 'add rule netdev reeldub_probe bound counter'
previous=0
for cycle in 1 2; do
    ip link add rdcheck0 type veth peer name rdpeer0
    ip link set rdcheck0 up
    ip link set rdpeer0 up
    ip address add 198.18.1.1/24 dev rdpeer0
    ping -c 1 -W 1 -I rdpeer0 198.18.1.2 >/dev/null || true
    count=$(nft list chain netdev reeldub_probe bound | awk '/counter packets/ {print $3}')
    [[ $count =~ ^[0-9]+$ && $count -gt $previous ]]
    previous=$count
    ip link del rdcheck0
done
NET
