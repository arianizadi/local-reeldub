#!/usr/bin/env bash
set -euo pipefail
umask 077
vault="$HOME/.local/share/reeldub-vault"
if mountpoint -q "$vault/open"; then
    # An interrupted FUSE daemon can leave an inaccessible mount behind.
    if [[ $(findmnt --mountpoint "$vault/open" -n -o FSTYPE) == fuse.gocryptfs ]] && ! stat "$vault/open/." >/dev/null 2>&1; then
        fusermount3 -uz "$vault/open"
    else
        echo "An existing filesystem is mounted at the ReelDub vault" >&2
        exit 1
    fi
fi
# The key is consumed through an inherited pipe. The main systemd process supervises FUSE.
exec 3< <(sg tss -c 'exec systemd-creds decrypt --name=vault.key "$HOME/.config/reeldub-origin/vault.cred" -')
exec "$HOME/.local/opt/reeldub-host/crypt-package/usr/bin/gocryptfs" -fg -q -passfile /dev/fd/3 "$vault/cipher" "$vault/open"
