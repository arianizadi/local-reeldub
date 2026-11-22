#!/usr/bin/env bash
# Inside the control guest: read the devbox's public key from stdin.
set -euo pipefail
source_ip=$(python3 -c 'import ipaddress,sys; a=ipaddress.IPv4Address(sys.argv[1]); assert a.is_private; print(a)' "${1:?devbox LAN IPv4}")
user=reeldub-op-tunnel
id -u "$user" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$user"
read -r algorithm key _ || true
[[ $algorithm == ssh-ed25519 && $key =~ ^[A-Za-z0-9+/=]+$ ]]
install -d -m 0755 /etc/reeldub /run/sshd
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
printf '%s %s\n' "$algorithm" "$key" > "$tmp/public"
ssh-keygen -lf "$tmp/public" >/dev/null
printf 'from="%s",restrict,port-forwarding,permitopen="127.0.0.1:8300",command="/bin/false" %s %s\n' \
    "$source_ip" "$algorithm" "$key" > "$tmp/authorized"
install -o root -g root -m 0644 "$tmp/authorized" /etc/reeldub/operator-authorized
cat > "$tmp/match" <<EOF
Match User $user
    AuthenticationMethods publickey
    AuthorizedKeysFile /etc/reeldub/operator-authorized
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:8300
    PermitListen none
    PermitTTY no
    PermitTunnel no
    PermitUserRC no
    X11Forwarding no
    MaxSessions 0
    ForceCommand /bin/false
EOF
install -o root -g root -m 0644 "$tmp/match" /etc/reeldub/operator-sshd.conf
include='Include /etc/reeldub/operator-sshd.conf'
cp /etc/ssh/sshd_config "$tmp/main"
grep -Fqx "$include" "$tmp/main" || printf '\n%s\n' "$include" >> "$tmp/main"
/usr/sbin/sshd -t -f "$tmp/main"
grep -Fqx "$include" /etc/ssh/sshd_config || printf '\n%s\n' "$include" >> /etc/ssh/sshd_config
/usr/sbin/sshd -t
effective=$(/usr/sbin/sshd -T -C "user=$user,addr=$source_ip,host=devbox")
for rule in 'allowtcpforwarding local' 'permitopen 127.0.0.1:8300' 'maxsessions 0' 'permitlisten none' 'forcecommand /bin/false'; do
    grep -Fqx "$rule" <<<"$effective"
done
if systemctl is-active --quiet ssh.service; then systemctl reload ssh.service; fi
