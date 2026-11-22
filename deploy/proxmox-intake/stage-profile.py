"""Read an inline OpenVPN profile from a zip on stdin. Never execute vendor directives."""
from io import BytesIO
from ipaddress import IPv4Address
from pathlib import Path
import os
import re
import shlex
import sys
import zipfile


def parse(profile):
    lines = profile.strip().splitlines()
    blocks, remote = {}, []
    outside = []
    while lines:
        row = lines.pop(0).strip()
        if row.startswith("<"):
            if row not in ("<ca>", "<cert>", "<key>", "<tls-auth>"):
                raise ValueError("Unsupported embedded material")
            tag = row[1:-1]
            if tag in blocks:
                raise ValueError("Duplicated embedded material")
            body = []
            while lines and lines[0].strip() != f"</{tag}>":
                body.append(lines.pop(0))
            if not lines or not body:
                raise ValueError("Incomplete embedded material")
            lines.pop(0)
            blocks[tag] = "\n".join(body)
        elif row and not row.startswith(("#", ";")):
            parts = shlex.split(row)
            outside.append(parts)
            if parts[0] == "remote":
                remote.append(parts[1:])
    if set(blocks) != {"ca", "cert", "key", "tls-auth"} or len(remote) != 1 or len(remote[0]) != 2:
        raise ValueError("Expected one IPv4 UDP server and four inline credentials")
    for requirement in (["proto", "udp"], ["dev", "tun"], ["remote-cert-tls", "server"],
                        ["key-direction", "1"], ["auth", "SHA256"]):
        if requirement not in outside:
            raise ValueError("Unsupported OpenVPN transport or authentication")
    addr, port = IPv4Address(remote[0][0]), int(remote[0][1])
    if not addr.is_global or not 1 <= port <= 65535:
        raise ValueError("The VPN endpoint must be public")
    names = {"ca": "CERTIFICATE", "cert": "CERTIFICATE", "key": "(?:RSA |EC )?PRIVATE KEY",
             "tls-auth": "OpenVPN Static key V1"}
    pieces = []
    for tag, name in names.items():
        pattern = rf"-----BEGIN ({name})-----\n([a-zA-Z0-9+/=\n]+)\n-----END \1-----"
        matches = list(re.finditer(pattern, blocks[tag].strip()))
        if len(matches) != 1:
            raise ValueError("Unsupported credential encoding")
        pieces.append(f"<{tag}>\n{matches[0].group()}\n</{tag}>\n")
    fixed = f"""client
dev tun0
proto udp4
remote {addr} {port}
nobind
remote-cert-tls server
tls-version-min 1.2
auth SHA256
auth-nocache
data-ciphers AES-256-GCM:AES-128-GCM:AES-256-CBC
data-ciphers-fallback AES-256-CBC
key-direction 1
route-nopull
redirect-gateway def1
persist-key
ping 10
ping-restart 60
verb 2
"""
    return fixed + "".join(pieces), f"{addr} {port}\n"


def main():
    directory = Path(sys.argv[1])
    os.umask(0o077)
    archive = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(archive) > 1024 * 1024:
        raise ValueError("Profile archive too large")
    with zipfile.ZipFile(BytesIO(archive)) as bundle:
        files = [entry for entry in bundle.infolist() if entry.filename.endswith(".conf")]
        if len(files) != 1 or files[0].file_size > 65536:
            raise ValueError("Expected one small inline .conf profile")
        config, endpoint = parse(bundle.read(files[0]).decode())
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for filename, content in (("client.conf", config), ("endpoint", endpoint)):
        with (directory / filename).open("x") as output:
            output.write(content)


if __name__ == "__main__":
    main()
