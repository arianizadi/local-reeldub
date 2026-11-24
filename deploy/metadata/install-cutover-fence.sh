#!/bin/sh
# Run on CT115 only after checking the new store and before reopening admissions.
set -eu
[ "$(id -u)" = 0 ]
database=${1:?Usage: install-cutover-fence.sh verified_mongo_database}
case "$database" in *[!a-z0-9_]*|admin|config|local) exit 2;; esac
dir=/etc/reeldub-metadata
install -d -m 0755 "$dir"
if [ -e "$dir/authority" ] && [ "$(cat "$dir/authority")" != "$database" ]; then
    echo "This host already has a different metadata authority. Reconcile before recovery." >&2
    exit 1
fi
temporary=$(mktemp "$dir/.authority.XXXXXX")
trap 'rm -f "$temporary"' EXIT
printf '%s\n' "$database" > "$temporary"
chmod 0444 "$temporary"
mv -T "$temporary" "$dir/authority"
install -Dm0755 "$(dirname "$0")/verify-authority.sh" /usr/local/libexec/reeldub/verify-authority
for service in api operator fleet; do
    dropin="/etc/systemd/system/reeldub-${service}.service.d"
    install -d -m 0755 "$dropin"
    cat > "$dropin/90-metadata-authority.conf" <<'UNIT'
[Service]
ExecStartPre=/usr/local/libexec/reeldub/verify-authority /etc/reeldub-metadata/authority
UNIT
    chmod 0644 "$dropin/90-metadata-authority.conf"
done
systemctl daemon-reload
