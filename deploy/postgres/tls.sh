#!/bin/sh
# Runs as postgres, offline, before the database listener starts.
set -eu
umask 077
issuer=/var/lib/reeldub-issuer
cd /var/lib/reeldub-tls
tmp=$(mktemp -d .tls.XXXXXXXX)
trap 'rm -rf "$tmp"' EXIT
if [ ! -f "$issuer/issuer.key" ] && [ ! -f "$issuer/issuer.pem" ]; then
    openssl req -new -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -noenc \
        -subj '/CN=ReelDub private database CA' -days 3650 \
        -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' \
        -keyout "$tmp/issuer.key" -out "$tmp/issuer.pem"
    mv "$tmp/issuer.key" "$issuer/issuer.key"
    mv "$tmp/issuer.pem" "$issuer/issuer.pem"
fi
test -s "$issuer/issuer.key" && test -s "$issuer/issuer.pem" || { echo 'Restore the complete private database CA from backup.' >&2; exit 1; }
if ! openssl x509 -in server.pem -checkend 2592000 -noout >/dev/null 2>&1 || [ ! -s server.key ]; then
    openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -noenc \
        -subj '/CN=postgres' -keyout "$tmp/server.key" -out "$tmp/server.csr"
    printf '%s\n' 'subjectAltName=DNS:postgres' 'basicConstraints=critical,CA:FALSE' \
        'keyUsage=critical,digitalSignature' 'extendedKeyUsage=serverAuth' > "$tmp/extensions"
    openssl x509 -req -in "$tmp/server.csr" -CA "$issuer/issuer.pem" -CAkey "$issuer/issuer.key" \
        -set_serial "0x$(openssl rand -hex 16)" -days 825 -extfile "$tmp/extensions" -out "$tmp/server.pem"
    mv "$tmp/server.key" server.key
    mv "$tmp/server.pem" server.pem
fi
openssl verify -purpose sslserver -verify_hostname postgres -CAfile "$issuer/issuer.pem" server.pem
# Only the public trust anchor leaves the private database certificate volume.
cp "$issuer/issuer.pem" /var/lib/reeldub-trust/ca.new
chmod 0644 /var/lib/reeldub-trust/ca.new
mv /var/lib/reeldub-trust/ca.new /var/lib/reeldub-trust/ca.pem
