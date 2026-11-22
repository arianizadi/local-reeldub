"""Run on PVE. The dedicated guest's veth is the only device this ruleset attaches to."""
from ipaddress import IPv4Address
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys


def main():
    guest = int(sys.argv[1])
    address, gateway, *peers = map(IPv4Address, sys.argv[2:])
    endpoint, raw_port = sys.stdin.read().split()
    endpoint, port = IPv4Address(endpoint), int(raw_port)
    if not peers or not all(ip.is_private for ip in (address, gateway, *peers)) or not endpoint.is_global or not 1 <= port <= 65535:
        raise ValueError("Expected private guest, gateway, callers and a public VPN endpoint")
    config = subprocess.check_output(["pct", "config", str(guest)], text=True)
    if "unprivileged: 1" not in config.splitlines():
        raise ValueError("Expected an unprivileged guest")
    net = next(line for line in config.splitlines() if line.startswith("net0: "))
    mac = re.search(r"hwaddr=((?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2})(?:,|$)", net).group(1)
    device = f"veth{guest}i0"
    table = f"reeldub_intake_{guest}"
    clients = ", ".join(map(str, peers))
    rules = f"""destroy table netdev {table}
table netdev {table} {{
 chain from_guest {{
  type filter hook ingress device "{device}" priority -450; policy drop;
  ether saddr != {mac} drop
  ether type arp arp saddr ether {mac} arp saddr ip {{ 0.0.0.0, {address} }} accept
  ip saddr {{ 0.0.0.0, {address} }} ip daddr {{ {gateway}, 255.255.255.255 }} udp sport 68 udp dport 67 accept
  ip saddr {address} ip daddr {endpoint} udp dport {port} accept
  ip saddr {address} ip daddr {{ {clients} }} tcp sport 8443 tcp flags & (syn | ack) != syn accept
 }}
 chain to_guest {{
  type filter hook egress device "{device}" priority 450; policy drop;
  ether type arp arp daddr ip {{ 0.0.0.0, {address} }} accept
  ip saddr {gateway} udp sport 67 udp dport 68 accept
  ip saddr {endpoint} ip daddr {address} udp sport {port} accept
  ip saddr {{ {clients} }} ip daddr {address} tcp dport 8443 accept
  ip daddr {address} icmp type destination-unreachable accept
 }}
}}
"""
    os.umask(0o077)
    probe = Path("/etc/reeldub-keys") / (table + "-preflight.sh")
    shutil.copyfile(Path(__file__).with_name("verify-netdev.sh"), probe)
    subprocess.run(["/bin/bash", str(probe)], check=True)
    path = Path(f"/etc/reeldub-keys/{table}.nft")
    temporary = path.with_suffix(".new")
    temporary.write_text(rules)
    subprocess.run(["nft", "--check", "-f", str(temporary)], check=True)
    temporary.replace(path)
    unit = f"reeldub-intake-netdev-{guest}.service"
    Path("/etc/systemd/system", unit).write_text(f"""[Unit]
Description=Interface-scoped ReelDub intake boundary for CT {guest}
After=nftables.service
Before=pve-guests.service
[Service]
Type=oneshot
RemainAfterExit=yes
RuntimeDirectory=reeldub-guard-{guest}
RuntimeDirectoryMode=0755
ExecStartPre=/bin/bash {probe}
ExecStart=/usr/sbin/nft -f {path}
ExecStartPost=/usr/bin/install -m 0444 /dev/null /run/reeldub-guard-{guest}/ready
ExecReload=/usr/sbin/nft -f {path}
[Install]
WantedBy=multi-user.target
""")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", unit], check=True)
    subprocess.run(["systemctl", "reload", unit], check=True)
    marker = Path(f"/run/reeldub-guard-{guest}")
    marker.mkdir(mode=0o755, exist_ok=True)
    (marker/"ready").touch()
    marker.chmod(0o755)
    (marker/"ready").chmod(0o444)
    bind = f"{marker},mp=/etc/reeldub-host-guard,ro=1"
    configured = next((line[5:] for line in config.splitlines() if line.startswith("mp1: ")), None)
    if configured and set(configured.split(",")) != set(bind.split(",")):
        raise RuntimeError("mp1 is already assigned")
    if not configured:
        subprocess.run(["pct", "set", str(guest), "--mp1", bind], check=True)


if __name__ == "__main__":
    main()
