"""Allow or revoke one client certificate serial at the private TLS listener."""
import fcntl
from pathlib import Path
import re
import subprocess
import sys
from tempfile import NamedTemporaryFile


def main():
    action, cert = sys.argv[1:]
    if action not in ("allow", "revoke"):
        raise ValueError("Use allow or revoke")
    tls = Path("/srv/reeldub-private/tls")
    if action == "revoke" and cert.startswith("serial:"):
        serial = cert.removeprefix("serial:").upper()
    else:
        raw = subprocess.check_output(["openssl", "x509", "-in", cert, "-noout", "-serial"], text=True)
        serial = raw.strip().removeprefix("serial=").upper()
    if action == "allow":
        subprocess.run(["openssl", "verify", "-purpose", "sslclient", "-CAfile", str(tls/"ca.pem"), cert],
                       check=True, stdout=subprocess.DEVNULL)
    if not re.fullmatch("[A-F0-9]+", serial):
        raise ValueError("Unexpected certificate serial")
    allowlist = tls / "clients.map"
    with (tls/"permits.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = set(allowlist.read_text().splitlines()) if allowlist.exists() else set()
        line = f'"{serial}" 1;'
        if action == "allow":
            rows.add(line)
        else:
            rows.discard(line)
        with NamedTemporaryFile(mode="w", dir=tls, delete=False) as new:
            new.writelines(row + "\n" for row in sorted(rows))
        Path(new.name).replace(allowlist)
        if subprocess.run(["systemctl", "is-active", "--quiet", "reeldub-intake-mtls"]).returncode == 0:
            subprocess.run(["nginx", "-t", "-q", "-c", "/etc/reeldub/nginx.conf"], check=True)
            subprocess.run(["systemctl", "kill", "--kill-whom=main", "-s", "HUP", "reeldub-intake-mtls"], check=True)


if __name__ == "__main__":
    main()
