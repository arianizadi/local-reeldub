"""Provision an isolated ReelDub MongoDB replica on the existing Proxmox Mongo guest.

Run as root on the Proxmox host. The existing Mongo service is left online.
The database guest and the control guest communicate over a pinned, restricted
loopback SSH forward. Credentials and database files reside on encrypted ZFS.
"""
import ipaddress
import os
from pathlib import Path
import re
import subprocess
import sys

DB_CT = str(int(os.environ["REELDUB_DB_CT"]))
CONTROL_CT = str(int(os.environ["REELDUB_CONTROL_CT"]))
PARENT = os.environ["REELDUB_ENCRYPTED_DATASET"]
DATASET = PARENT + "/metadata"
STORAGE = "/var/lib/reeldub-private/metadata"
MOUNT = "/srv/reeldub-metadata"
CFG = "/srv/reeldub-private/config"
DB_IP = str(ipaddress.IPv4Address(os.environ["REELDUB_DB_IP"]))
CONTROL_IP = str(ipaddress.IPv4Address(os.environ["REELDUB_CONTROL_IP"]))
if DB_CT == CONTROL_CT or not re.fullmatch(r"[\w./-]+", PARENT):
    sys.exit("Invalid guest or storage settings")


def run(*args, data=None, capture=False):
    return subprocess.run(args, input=data, stdout=subprocess.PIPE if capture else None, check=True).stdout


def guest(ct, script, capture=False):
    return run("pct", "exec", ct, "--", "bash", "-seu", data=script.encode(), capture=capture)


def transfer(src, source, dst, dest, owner="root:root", mode="0600"):
    content = run("pct", "exec", src, "--", "cat", source, capture=True)
    run("pct", "exec", dst, "--", "sh", "-ec",
        'umask 077; t=$(mktemp "${1}.XXXXXX"); cat > "$t"; chown "$2" "$t"; chmod "$3" "$t"; mv -fT "$t" "$1"',
        "install", dest, owner, mode, data=content)


def storage():
    enc = run("zfs", "get", "-Ho", "value", "encryption", PARENT, capture=True).decode().strip()
    if enc in {"off", "-"}:
        sys.exit("Parent dataset must be encrypted")
    if subprocess.run(["zfs", "list", "-H", DATASET], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        run("zfs", "create", "-o", "mountpoint=" + STORAGE, "-o", "quota=8G", "-o", "compression=lz4", DATASET)
    if run("zfs", "get", "-Ho", "value", "mountpoint", DATASET, capture=True).decode().strip() != STORAGE:
        sys.exit("Existing dataset has an unexpected mountpoint")
    service = "reeldub-metadata-storage.service"
    Path("/etc/systemd/system", service).write_text(f"""[Unit]
Description=Mount encrypted ReelDub account database
Requires=reeldub-control-storage.service
After=reeldub-control-storage.service
Before=pve-container@{DB_CT}.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -ec 'mountpoint -q {STORAGE} || zfs mount {DATASET}; test "$(findmnt -nro SOURCE -M {STORAGE})" = "{DATASET}"'
""")
    directory = Path(f"/etc/systemd/system/pve-container@{DB_CT}.service.d")
    directory.mkdir(exist_ok=True)
    # Failure to unlock ReelDub storage must not disable the pre-existing shared Mongo service.
    (directory / "reeldub-metadata.conf").write_text(f"[Unit]\nWants={service}\nAfter={service}\n")
    run("systemctl", "daemon-reload")
    run("systemctl", "start", service)
    host_marker = Path(STORAGE, ".reeldub-metadata-volume")
    if not host_marker.exists():
        import uuid
        host_marker.write_text(str(uuid.uuid4()) + "\n")
    live_config = run("pct", "config", DB_CT, capture=True).decode()
    attachment = next((line for line in live_config.splitlines() if f"mp={MOUNT}" in line), None)
    if attachment and not attachment.split(": ", 1)[1].startswith(STORAGE + ","):
        sys.exit("Unexpected existing mount at the database location")
    if not attachment:
        number = next(n for n in range(16) if not re.search(rf"^mp{n}:", live_config, re.M))
        run("pct", "set", DB_CT, f"-mp{number}", f"{STORAGE},mp={MOUNT},backup=0")
    # Proxmox uses mapped root (100000) for these unprivileged guests.
    config = run("pct", "config", DB_CT, capture=True).decode()
    if "unprivileged: 1" not in config or "lxc.idmap" in config:
        sys.exit("Verify guest UID mapping before granting access to the private dataset")
    os.chown(STORAGE, 100000, 100000)
    os.chmod(STORAGE, 0o711)
    guest(DB_CT, f'mountpoint -q {MOUNT}; test "$(cat {MOUNT}/.reeldub-metadata-volume)" = "{host_marker.read_text().strip()}"')


def database():
    guest(DB_CT, r"""
mountpoint -q /srv/reeldub-metadata
root=/srv/reeldub-metadata
getent group reeldub-meta >/dev/null || groupadd --system reeldub-meta
id reeldub-meta >/dev/null 2>&1 || useradd --system --no-create-home -g reeldub-meta -s /usr/sbin/nologin reeldub-meta
install -d -m 0700 -o reeldub-meta -g reeldub-meta "$root/data"
install -d -m 0710 -o root -g reeldub-meta "$root/tls"
install -d -m 0700 "$root/credentials"
umask 077
cd "$root/tls"
if [[ ! -s ca.pem ]]; then
 openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj '/CN=ReelDub Metadata CA' \
  -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' -keyout ca.key -out ca.pem 2>/dev/null
fi
if [[ ! -s server.pem ]]; then
 openssl req -new -newkey rsa:3072 -nodes -subj '/CN=localhost' -keyout server.key -out server.csr 2>/dev/null
 openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 825 \
  -extfile <(printf 'subjectAltName=IP:127.0.0.1,DNS:localhost\nextendedKeyUsage=serverAuth,clientAuth\nkeyUsage=digitalSignature,keyEncipherment\n') -out server.crt
 cat server.key server.crt > server.pem
fi
[[ -s replica.key ]] || openssl rand -base64 384 > replica.key
chown root:reeldub-meta ca.pem server.pem
chmod 0640 ca.pem server.pem
chown reeldub-meta:reeldub-meta replica.key
chmod 0400 replica.key
cd "$root/credentials"
for user in admin app fleet backup; do [[ -s $user.pw ]] || openssl rand -hex 32 > "$user.pw"; done
cat > /etc/systemd/system/reeldub-metadata.service <<'UNIT'
[Unit]
Description=ReelDub private MongoDB metadata replica
After=network.target
[Service]
User=reeldub-meta
Group=reeldub-meta
ExecStartPre=/usr/bin/mountpoint -q /srv/reeldub-metadata
ExecStartPre=/usr/bin/cmp -s /etc/reeldub-metadata-volume /srv/reeldub-metadata/.reeldub-metadata-volume
ExecStart=/usr/bin/mongod --dbpath /srv/reeldub-metadata/data --bind_ip 127.0.0.1 --port 29361 --nounixsocket --replSet reeldub-meta --auth --keyFile /srv/reeldub-metadata/tls/replica.key --tlsMode requireTLS --tlsCertificateKeyFile /srv/reeldub-metadata/tls/server.pem --tlsCAFile /srv/reeldub-metadata/tls/ca.pem --tlsAllowConnectionsWithoutCertificates --wiredTigerCacheSizeGB 0.4
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=yes
CapabilityBoundingSet=
LimitCORE=0
[Install]
WantedBy=multi-user.target
UNIT
if [[ ! -f /etc/reeldub-metadata-volume ]]; then install -m 0444 "$root/.reeldub-metadata-volume" /etc/reeldub-metadata-volume; fi
systemctl daemon-reload
systemctl enable --now reeldub-metadata
mongo=(mongosh --quiet --norc --host 127.0.0.1 --port 29361 --tls --tlsCAFile "$root/tls/ca.pem")
for n in {1..20}; do "${mongo[@]}" --eval 'db.runCommand({ping:1}).ok' >/dev/null 2>&1 && break; sleep 1; done
cat > bootstrap.js <<'JS'
const admin = db.getSiblingDB('admin'), store = db.getSiblingDB('reeldub_metadata');
const pw = (name) => fs.readFileSync('/srv/reeldub-metadata/credentials/'+name+'.pw','utf8').trim();
if (!admin.runCommand({hello:1}).setName) {
  const v=admin.runCommand({replSetInitiate:{_id:'reeldub-meta',members:[{_id:0,host:'localhost:29361'}]}});
  if(!v.ok && v.codeName!=='AlreadyInitialized') throw Error('replica bootstrap failed');
}
for(let i=0;i<50 && !admin.runCommand({hello:1}).isWritablePrimary;i++) sleep(300);
try { if(!admin.auth('reeldub_provisioner',pw('admin'))) throw Error('bootstrap'); }
catch(e) { admin.createUser({user:'reeldub_provisioner',pwd:pw('admin'),roles:['root']}); admin.auth('reeldub_provisioner',pw('admin')); }
const grants = [
 ['jobs',['find','update']], ['sources',['find','insert','update','remove']], ['account_deletions',['find']], ['fleet_claim_locks',['find','insert','update','remove']]
].map(([collection,actions])=>({resource:{db:'reeldub_metadata',collection},actions}));
if (store.getRole('queue_coordinator')) store.updateRole('queue_coordinator',{privileges:grants,roles:[]});
else store.createRole({role:'queue_coordinator',privileges:grants,roles:[]});
const roles = {app:['readWrite'], fleet:['queue_coordinator'], backup:['read']};
for (const [name, role] of Object.entries(roles)) {
 if (!store.getUser(name)) store.createUser({user:name,pwd:pw(name),roles:role});
 else store.updateUser(name,{roles:role});
}
JS
"${mongo[@]}" --file bootstrap.js >/dev/null
for user in app fleet backup; do
 printf 'mongodb://%s:%s@127.0.0.1:29361/reeldub_metadata?authSource=reeldub_metadata&tls=true&replicaSet=reeldub-meta&directConnection=true&w=majority&journal=true&retryWrites=true\n' "$user" "$(cat "$user.pw")" > "$user.uri"
done
""")


def connect():
    # Generate the tunnel credential where it stays, and pin the database guest's host key.
    guest(CONTROL_CT, f"""
mountpoint -q /srv/reeldub-private
getent group reeldub-db-link >/dev/null || groupadd --system reeldub-db-link
id reeldub-db-link >/dev/null 2>&1 || useradd --system --no-create-home -g reeldub-db-link -s /usr/sbin/nologin reeldub-db-link
install -d -o reeldub-db-link -g reeldub-db-link -m 0700 {CFG}/metadata-link
cd {CFG}/metadata-link
[[ -f ssh.key ]] || runuser -u reeldub-db-link -- ssh-keygen -q -N '' -t ed25519 -f "$PWD/ssh.key"
""")
    pub = run("pct","exec",CONTROL_CT,"--","cat",CFG+"/metadata-link/ssh.key.pub",capture=True).decode().split()
    host = run("pct","exec",DB_CT,"--","cat","/etc/ssh/ssh_host_ed25519_key.pub",capture=True).decode().split()
    if any(k[0] != "ssh-ed25519" or not re.fullmatch(r"[a-zA-Z0-9+/=]+", k[1]) for k in (pub,host)):
        sys.exit("Unexpected SSH key encoding")
    guest(DB_CT, f"""
if ! id reeldub-forward >/dev/null 2>&1; then
 useradd -m -s /usr/sbin/nologin reeldub-forward
fi
install -d -o reeldub-forward -g reeldub-forward -m 0700 /home/reeldub-forward/.ssh
printf '%s\n' 'from="{CONTROL_IP}",restrict,port-forwarding,permitopen="127.0.0.1:29361",command="/bin/false" {pub[0]} {pub[1]}' > /home/reeldub-forward/.ssh/authorized_keys
chown reeldub-forward:reeldub-forward /home/reeldub-forward/.ssh/authorized_keys
chmod 0600 /home/reeldub-forward/.ssh/authorized_keys
conf=/etc/ssh/sshd_config.d/90-reeldub-forward.conf
tmp=$(mktemp)
cat > "$tmp" <<'EOF'
Match User reeldub-forward
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:29361
    PermitTunnel no
    X11Forwarding no
    AllowAgentForwarding no
    PermitTTY no
    PasswordAuthentication no
    KbdInteractiveAuthentication no
Match all
EOF
if ! cmp -s "$tmp" "$conf"; then
 install -m 0644 "$tmp" "$conf"
 sshd -t
 if systemctl is-active --quiet ssh.socket; then systemctl restart ssh.service; else systemctl reload ssh; fi
fi
rm -f "$tmp"
systemctl start ssh.service
""")
    guest(CONTROL_CT, f"""
umask 077
cd {CFG}/metadata-link
printf '%s\n' '{DB_IP} {host[0]} {host[1]}' > known_hosts
chown reeldub-db-link:reeldub-db-link known_hosts
cat > /etc/systemd/system/reeldub-metadata-link.service <<'EOF'
[Unit]
Description=Private ReelDub metadata connection
RequiresMountsFor=/srv/reeldub-private
After=network-online.target
Wants=network-online.target
[Service]
User=reeldub-db-link
Group=reeldub-db-link
ExecStart=/usr/bin/ssh -F /dev/null -NT -i {CFG}/metadata-link/ssh.key -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null -o UserKnownHostsFile={CFG}/metadata-link/known_hosts -o ExitOnForwardFailure=yes -o IdentitiesOnly=yes -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=2 -o ConnectTimeout=8 -L 127.0.0.1:29361:127.0.0.1:29361 reeldub-forward@{DB_IP}
Restart=always
RestartSec=3
NoNewPrivileges=yes
CapabilityBoundingSet=
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now reeldub-metadata-link
install -d -m 0700 /etc/reeldub-backup
""")
    transfer(DB_CT, MOUNT+"/tls/ca.pem", CONTROL_CT, CFG+"/metadata-ca.pem", mode="0644")
    for name, role in (("app","reeldub-api"), ("operator","reeldub-operator"), ("fleet","reeldub-fleet"), ("backup","root")):
        user = "app" if name == "operator" else name
        transfer(DB_CT, MOUNT+"/credentials/"+user+".uri", CONTROL_CT, CFG+"/metadata-"+name+".uri", "root:"+role,
                 "0600" if role=="root" else "0640")
    guest(CONTROL_CT, f"""
if [[ ! -e /etc/reeldub-backup/mongo-uri ]]; then ln -s {CFG}/metadata-backup.uri /etc/reeldub-backup/mongo-uri; fi
for n in {{1..10}}; do if timeout 3 openssl s_client -connect 127.0.0.1:29361 -verify_ip 127.0.0.1 -CAfile {CFG}/metadata-ca.pem -verify_return_error </dev/null 2>/dev/null | grep -q 'Verify return code: 0'; then break; fi; sleep 1; done
/opt/reeldub/venv/bin/python - <<'CHECK'
from pathlib import Path
from pymongo import MongoClient
p=Path('{CFG}')
for name in ('app','operator','fleet','backup'):
 with MongoClient((p/f'metadata-{{name}}.uri').read_text().strip(),tlsCAFile=str(p/'metadata-ca.pem'),serverSelectionTimeoutMS=5000) as c:
  assert c.admin.command('hello')['isWritablePrimary']
  c.reeldub_metadata.jobs.find_one({{}},{{'_id':1}})
print('Verified TLS, replica primary and four restricted credential files.')
CHECK
""")


if __name__ == "__main__":
    storage()
    database()
    connect()
