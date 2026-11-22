"""Install a verified fresh client identity atomically. The old identity remains in the other directory."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys


def public_key(path, cert=False):
    if cert:
        return subprocess.check_output(["openssl", "x509", "-in", path, "-noout", "-pubkey"])
    return subprocess.check_output(["openssl", "pkey", "-in", path, "-pubout"])


def main():
    active, staged = map(Path, sys.argv[1:])
    if active.is_symlink() or staged.is_symlink() or active.resolve() == staged.resolve():
        raise ValueError("Expected two distinct private directories")
    if any((p/f).is_symlink() for p in (active, staged) for f in ("ca.pem", "client.pem", "client.key")):
        raise ValueError("Client identity files must be regular files")
    if (active/"ca.pem").read_bytes() != (staged/"ca.pem").read_bytes():
        raise ValueError("Renewal must use the existing intake authority")
    subprocess.run(["openssl", "verify", "-purpose", "sslclient", "-CAfile", staged/"ca.pem", staged/"client.pem"], check=True)
    if public_key(staged/"client.key") != public_key(staged/"client.pem", cert=True):
        raise ValueError("Issued certificate does not match the staged key")
    if public_key(staged/"client.key") == public_key(active/"client.key"):
        raise ValueError("Generate a fresh private key before rotating")
    for path in (staged, *(staged/f for f in ("ca.pem", "client.pem", "client.key"))):
        old = active if path == staged else active/path.name
        metadata = old.stat()
        os.chown(path, metadata.st_uid, metadata.st_gid)
        os.chmod(path, metadata.st_mode & 0o777)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(active), -100, os.fsencode(staged), 2):
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


if __name__ == "__main__":
    main()
