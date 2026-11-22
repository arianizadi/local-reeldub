#!/usr/bin/env bash
# Private keys are generated on the endpoint that owns them. Only CSRs and public certificates travel.
set -euo pipefail
umask 077
command=${1:?choose authority, request, or sign}
case $command in
authority)
    directory=${2:?host-only encrypted authority directory}
    install -d -m 0700 "$directory"
    cd "$directory"
    if [[ ! -e ca.key && ! -e ca.pem ]]; then
        openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1825 -sha256 \
            -subj '/CN=ReelDub Intake Private CA' -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
            -addext 'keyUsage=critical,keyCertSign,cRLSign' -keyout ca.key -out ca.pem
    fi
    test -s ca.key && test -s ca.pem
    ;;
request)
    role=${2:?client or server}
    directory=${3:?encrypted key directory}
    name=${4:?certificate name}
    [[ $role == server || $role == client ]]
    [[ $name =~ ^[a-zA-Z0-9.-]+$ ]]
    mkdir -p "$directory"
    cd "$directory"
    [[ ! -e $role.key && ! -e $role.csr ]] || { echo 'Generate rotations in a new private directory.' >&2; exit 1; }
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$role.key"
    openssl req -new -key "$role.key" -subj "/CN=$name" -out "$role.csr"
    cat "$role.csr"
    ;;
sign)
    directory=${2:?host-only encrypted CA directory}
    role=${3:?client or server}
    [[ $role == server || $role == client ]]
    cd "$directory"
    exec 9>issuance.lock
    flock -x 9
    tmp=$(mktemp -d)
    trap 'rm -rf -- "$tmp"' EXIT
    cat > "$tmp/request"
    openssl req -in "$tmp/request" -verify -noout >&2
    printf '%s\n' 'basicConstraints=critical,CA:FALSE' 'keyUsage=critical,digitalSignature' "extendedKeyUsage=${role}Auth" > "$tmp/ext"
    if [[ $role == server ]]; then
        address=$(python3 -c 'import ipaddress,sys; print(ipaddress.IPv4Address(sys.argv[1]))' "${4:?guest IPv4}")
        printf 'subjectAltName=DNS:reeldub-intake.internal,IP:%s\n' "$address" >> "$tmp/ext"
    fi
    openssl x509 -req -in "$tmp/request" -CA ca.pem -CAkey ca.key -CAcreateserial -sha256 -days 365 \
        -extfile "$tmp/ext" -out "$tmp/issued"
    openssl verify -purpose "ssl$role" -CAfile ca.pem "$tmp/issued" >&2
    printf '%s %s\n' "$(date -u +%FT%TZ)" "$(openssl x509 -in "$tmp/issued" -noout -serial -subject -enddate | paste -sd ' ' -)" >> issued.log
    cat "$tmp/issued"
    ;;
*) echo 'Expected authority, request, or sign' >&2; exit 2 ;;
esac
