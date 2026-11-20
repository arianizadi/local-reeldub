#!/usr/bin/env bash
set -euo pipefail
umask 077
dest=${1:?Usage: setup-mongo-tls.sh /private/path/new-directory}
mkdir -p "$(dirname "$dest")"
mkdir -m 700 "$dest"
cd "$dest"
openssl req -x509 -newkey rsa:3072 -nodes -days 1825 -subj "/CN=ReelDub private database CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout ca.key -out ca.pem
openssl req -new -newkey rsa:3072 -nodes -subj "/CN=mongo" -keyout server.key -out server.csr
cat > server.ext <<'CERT'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=DNS:mongo,DNS:localhost,IP:127.0.0.1
CERT
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 365 -sha256 -extfile server.ext -out server.crt
openssl verify -CAfile ca.pem server.crt
cat server.crt server.key >server.pem
chmod 0444 ca.pem server.pem
# Only these two files are mounted. The outer directory remains private.
rm server.key server.csr server.ext
