"""Install the intake guest's dedicated nftables table atomically."""
from ipaddress import IPv4Address
from pathlib import Path
import pwd
import subprocess


def main():
    root = Path("/srv/reeldub-private")
    server, raw_port = (root / "vpn/endpoint").read_text().split()
    endpoint, port = IPv4Address(server), int(raw_port)
    peers = [IPv4Address(line) for line in Path("/etc/reeldub/intake-peers").read_text().split()]
    if not endpoint.is_global or not 1 <= port <= 65535 or not peers or any(not peer.is_private for peer in peers):
        raise ValueError("Invalid VPN endpoint or trusted intake client")
    clients = ", ".join(map(str, peers))
    network_user = pwd.getpwnam("systemd-network").pw_uid
    egress_user = pwd.getpwnam("reeldub-egress").pw_uid
    rules = f"""
destroy table inet reeldub_intake
table inet reeldub_intake {{
 set clients {{ type ipv4_addr; elements = {{ {clients} }}; }}
 chain input {{
  type filter hook input priority -10; policy drop;
  iifname "lo" accept
  iifname "eth0" udp sport 67 udp dport 68 accept
  iifname "eth0" ip saddr {endpoint} udp sport {port} ct state established accept
  iifname "eth0" ip saddr @clients tcp dport 8443 ct state {{ new, established }} accept
  iifname "tun0" ct state established,related accept
  ip protocol icmp ct state related accept
 }}
 chain forward {{ type filter hook forward priority -10; policy drop; }}
 chain output {{
  type filter hook output priority -10; policy drop;
  oifname "lo" accept
  oifname "eth0" meta skuid {{ 0, {network_user} }} udp sport 68 udp dport 67 accept
  oifname "eth0" meta skuid 0 ip daddr {endpoint} udp dport {port} accept
  oifname "eth0" ip daddr @clients tcp sport 8443 ct state established accept
  oifname "tun0" meta skuid {egress_user} ip daddr 1.1.1.1 udp dport 53 accept
  oifname "tun0" meta skuid {egress_user} ip daddr 1.1.1.1 tcp dport 53 accept
  oifname "tun0" ip daddr {{ 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 }} drop
  oifname "tun0" meta skuid {egress_user} ip protocol tcp tcp dport 443 accept
 }}
}}
"""
    subprocess.run(["nft", "--check", "-f", "-"], input=rules, text=True, check=True)
    subprocess.run(["nft", "-f", "-"], input=rules, text=True, check=True)


if __name__ == "__main__":
    main()
