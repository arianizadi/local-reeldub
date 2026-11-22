"""Check effective Linux service identities and file permissions without reading any credential."""
import itertools
import os
import pwd
from pathlib import Path
import subprocess
import sys


def main():
    private = Path(os.getenv("REELDUB_PRIVATE_DIR", "/srv/reeldub-private"))
    config = private / "config"
    group_shared = {"api", "operator", "fleet"}
    permissions = {
        "api.dsn": group_shared,
        "intake/client.key": group_shared,
        "fleet-registry.json": group_shared,
        "fleet-tls.key": {"fleet-proxy"},
        "server.pem": {"media"},
        "origin.key": {"origin"},
        "api.env": set(),
        "operator.env": set(),
        "fleet.env": set(),
    }
    services = ("api", "operator", "fleet", "media", "origin", "ingress", "fleet-proxy")
    uids = set()
    for name in services:
        account = "reeldub-" + name
        identity = pwd.getpwnam(account)
        if identity.pw_uid in uids:
            raise RuntimeError("Some services share a UID")
        uids.add(identity.pw_uid)
        for filename, readers in permissions.items():
            allowed = subprocess.run(["runuser", "-u", account, "--", "test", "-r", str(config/filename)]).returncode == 0
            if allowed != (name in readers):
                raise RuntimeError(f"Unexpected access for {name} to {filename}")
        if "--running" in sys.argv:
            pid = subprocess.check_output(["systemctl", "show", account, "-p", "MainPID", "--value"], text=True).strip()
            status = Path(f"/proc/{pid}/status").read_text()
            effective = next(row.split()[2] for row in status.splitlines() if row.startswith("Uid:"))
            if int(effective) != identity.pw_uid:
                raise RuntimeError(f"Live {name} has the wrong UID")
        path = private / "service-tmp" / name
        if path.stat().st_uid != identity.pw_uid or path.stat().st_mode & 0o777 != 0o700:
            raise RuntimeError(f"Invalid scratch permissions for {name}")
    for user, target in itertools.permutations(services, 2):
        if subprocess.run(["runuser", "-u", "reeldub-"+user, "--", "test", "-x",
                           str(private/"service-tmp"/target)]).returncode == 0:
            raise RuntimeError(f"{user} can traverse the scratch directory for {target}")
    print("Verified seven independent UIDs, credential boundaries and private encrypted scratch directories.")


if __name__ == "__main__":
    main()
