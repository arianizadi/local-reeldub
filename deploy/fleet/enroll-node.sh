#!/usr/bin/env bash
# Run as root on the private coordinator. Transfer only the generated node directory.
set -euo pipefail
umask 077
cfg=${REELDUB_SECRET_DIR:?Set the private encrypted configuration directory}
address=${REELDUB_FLEET_IP:?Set the private coordinator IPv4 address}
node=${1:?Specify a node ID}
slots=${2:-1}
[[ $node =~ ^[a-z0-9][a-z0-9-]{0,48}$ && $slots =~ ^[12]$ && $address =~ ^[0-9.]+$ ]]
pki="$cfg/pki"
mkdir -p -m 0700 "$pki" "$cfg/enroll"
if [[ ! -f $pki/ca.pem ]]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1825 -sha256 -subj /CN=ReelDub-Fleet-CA -keyout "$pki/ca.key" -out "$pki/ca.pem"
fi
issue() {
  local dest=$1 cn=$2 usage=$3
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$cn" -keyout "$dest.key" -out "$dest.csr"
  openssl x509 -req -in "$dest.csr" -CA "$pki/ca.pem" -CAkey "$pki/ca.key" -CAcreateserial -days 365 -sha256 -extfile <(printf '%s\n' "basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature" "$usage") -out "$dest.pem"
  rm "$dest.csr"
}
if [[ ! -f $cfg/fleet-tls.pem ]]; then
  issue "$cfg/fleet-tls" control "$(printf 'extendedKeyUsage=serverAuth\nsubjectAltName=IP:%s' "$address")"
  install -m 0644 "$pki/ca.pem" "$cfg/fleet-ca.pem"
fi
dir="$cfg/enroll/$node"
[[ ! -e $dir ]] || { echo "Enrollment already exists" >&2; exit 2; }
mkdir -m 0700 "$dir"
issue "$dir/client" "$node" "extendedKeyUsage=clientAuth"
install -m 0644 "$pki/ca.pem" "$dir/ca.pem"
python3 - "$cfg" "$node" "$slots" <<'PY'
import fcntl,hashlib,json,os,secrets,sys
from pathlib import Path
cfg, node, slots = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
token = secrets.token_urlsafe(48)
with (cfg/"fleet-enrollment.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    path = cfg/"fleet-registry.json"
    registry = json.loads(path.read_text()) if path.exists() else {"nodes":{}}
    if node in registry["nodes"]:
        raise SystemExit("Choose an unused node ID")
    (cfg/"enroll"/node/"token").write_text(token)
    registry["nodes"][node] = {"token_sha256":hashlib.sha256(token.encode()).hexdigest(),"slots":slots,"studio":True}
    tmp = path.with_suffix(".new")
    tmp.write_text(json.dumps(registry))
    if path.exists(): os.chown(tmp,path.stat().st_uid,path.stat().st_gid)
    tmp.chmod(0o640)
    tmp.replace(path)
PY
