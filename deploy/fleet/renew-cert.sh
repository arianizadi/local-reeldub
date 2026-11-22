#!/usr/bin/env bash
# Renew a node certificate from its CSR, or renew the listener certificate in place.
set -euo pipefail
umask 077
cfg=${REELDUB_SECRET_DIR:?Encrypted coordinator configuration directory}
pki=${REELDUB_FLEET_CA_DIR:-$cfg/pki}
role=${1:?Specify node or server}
tmp=$(mktemp -d "$cfg/.renew.XXXXXXXX")
trap 'rm -rf -- "$tmp"' EXIT
sign() {
    openssl x509 -req -in "$1" -CA "$pki/ca.pem" -CAkey "$pki/ca.key" -set_serial "0x$(openssl rand -hex 16)" -days 365 -sha256 -extfile "$tmp/extensions" -out "$tmp/result.pem"
    openssl verify -CAfile "$pki/ca.pem" "$tmp/result.pem" >&2
}
printf '%s\n' 'basicConstraints=critical,CA:FALSE' 'keyUsage=critical,digitalSignature' > "$tmp/extensions"
case "$role" in
    node)
        node=${2:?Specify enrolled node ID}
        csr=${3:?Specify a CSR from this worker}
        [[ $node =~ ^[a-z0-9][a-z0-9-]{0,48}$ ]]
        python3 -c 'import json,sys; assert sys.argv[1] in json.load(open(sys.argv[2]))["nodes"]' "$node" "$cfg/fleet-registry.json"
        [[ $(openssl req -in "$csr" -noout -subject -nameopt RFC2253) == "subject=CN=$node" ]]
        openssl req -in "$csr" -noout -verify
        printf '%s\n' 'extendedKeyUsage=clientAuth' >> "$tmp/extensions"
        sign "$csr"
        cat "$tmp/result.pem"
        ;;
    server)
        ip=${REELDUB_FLEET_IP:?Specify coordinator IPv4 address}
        python3 -c 'import sys,ipaddress; ipaddress.IPv4Address(sys.argv[1])' "$ip"
        openssl req -new -key "$cfg/fleet-tls.key" -subj /CN=control -out "$tmp/server.csr"
        printf '%s\n' 'extendedKeyUsage=serverAuth' "subjectAltName=IP:$ip" >> "$tmp/extensions"
        sign "$tmp/server.csr"
        chown --reference="$cfg/fleet-tls.pem" "$tmp/result.pem"
        chmod --reference="$cfg/fleet-tls.pem" "$tmp/result.pem"
        mv -f "$tmp/result.pem" "$cfg/fleet-tls.pem"
        ;;
    *) exit 2 ;;
esac
