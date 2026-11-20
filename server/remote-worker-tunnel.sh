#!/bin/sh
# Keep the private DB tunnel and exactly one systemd worker across SSH reconnects.
set -eu
host=${1:?SSH host alias required}
credentials=${2:?Private worker environment file required}
forward=${3:?Loopback port and database forward required}
exec /usr/bin/ssh -xT -o BatchMode=yes -o ExitOnForwardFailure=yes -o ForwardAgent=no \
    -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -o ConnectTimeout=8 \
    -R "$forward" "$host" '
    set -eu
    umask 077
    dir="${XDG_RUNTIME_DIR:?}/reeldub-credentials"
    mkdir -p -m 700 "$dir"
    tmp=$(mktemp "$dir/worker.XXXXXX")
    trap '\''rm -f "$tmp"'\'' EXIT
    cat > "$tmp"
    test -s "$tmp"
    mv "$tmp" "$dir/worker.env"
    systemctl --user start reeldub-worker.service
    exec sleep infinity' < "$credentials"
