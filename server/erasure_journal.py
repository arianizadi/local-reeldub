"""Independent, bounded deletion receipts for restoring account metadata backups."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


RETENTION = timedelta(days=14)


def subject_hash(owner):
    return hashlib.sha256(("reeldub/erasure/v1/" + owner).encode()).hexdigest()


def receipt_name(stamp, digest):
    elapsed = stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
    micros = (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
    return f"{micros}-{digest}.json"


def record(owner, stamp):
    directory = os.getenv("REELDUB_ERASURE_JOURNAL_DIR")
    mirror = os.getenv("REELDUB_ERASURE_MIRROR_COMMAND")
    if not directory:
        if mirror:
            raise OSError("Erasure journal directory is missing")
        return
    folder = Path(directory)
    if not folder.is_absolute() or (mirror and not Path(mirror).is_absolute()):
        raise OSError("Erasure journal configuration requires absolute paths")
    digest = subject_hash(owner)
    target = folder / receipt_name(stamp, digest)
    data = json.dumps({"version": 1, "deleted_at": stamp.isoformat(), "subject_hash": digest},
                      sort_keys=True).encode()
    # The mirror needs the final basename. Only publish the local receipt when every
    # configured copy is durable, so a rejected request cannot act on a later restore.
    with tempfile.TemporaryDirectory(dir=folder, prefix=".pending-") as staging:
        candidate = Path(staging) / target.name
        with candidate.open("xb") as temporary:
            os.fchmod(temporary.fileno(), 0o600)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        if mirror:
            subprocess.run([mirror, str(candidate)], check=True, timeout=30, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.link(candidate, target)
        except FileExistsError:
            if target.read_bytes() != data:
                raise OSError("Conflicting erasure receipt")
        fd = os.open(folder, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read_receipt(path):
    receipt = json.loads(path.read_bytes())
    stamp = datetime.fromisoformat(receipt["deleted_at"])
    digest = receipt["subject_hash"]
    if (receipt["version"] != 1 or stamp.utcoffset() != timedelta(0) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or path.name != receipt_name(stamp, digest)):
        raise ValueError("Invalid erasure receipt")
    return stamp, digest


def prune(stamp):
    directory = os.getenv("REELDUB_ERASURE_JOURNAL_DIR")
    if directory:
        for receipt in Path(directory).glob("*.json"):
            created, _ = read_receipt(receipt)
            if created < stamp - RETENTION:
                receipt.unlink(missing_ok=True)
