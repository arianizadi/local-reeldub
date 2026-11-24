#!/usr/bin/env python3
"""Create write-once S3 snapshots and replay erasures on isolated disaster restores."""
import argparse
import base64
from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
import subprocess
from zipfile import ZipFile
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


RECEIPT = re.compile(r"[0-9]{15,18}-[0-9a-f]{64}\.json\Z")
SNAPSHOT = re.compile(r"snapshots/(?:daily|quarter-hour)/[0-9TZ-]+-[0-9a-f]{32}\.zip\Z")
COVERAGE = "policy/erasures-enforced-v1.json"


def s3():
    return boto3.client("s3", region_name=os.environ["AWS_REGION"],
                        config=Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 2}))


def bucket_args():
    return {"Bucket": os.environ["REELDUB_BACKUP_BUCKET"],
            "ExpectedBucketOwner": os.environ["REELDUB_BACKUP_AWS_ACCOUNT"]}


def put_once(client, key, data, checksum, *, idempotent=False):
    try:
        result = client.put_object(**bucket_args(), Key=key, Body=data, ChecksumSHA256=checksum,
                                   ServerSideEncryption="AES256", IfNoneMatch="*")
    except ClientError as exc:
        if idempotent and exc.response["ResponseMetadata"]["HTTPStatusCode"] == 412:
            return
        raise
    if result.get("ChecksumSHA256") != checksum or result.get("ServerSideEncryption") != "AES256":
        raise ValueError("S3 did not verify the expected checksum and encryption")


def check_receipt(name, data):
    if not RECEIPT.fullmatch(name) or len(data) > 1024:
        raise ValueError("Malformed deletion receipt")
    info = json.loads(data)
    stamp = datetime.fromisoformat(info["deleted_at"])
    micro, subject = name[:-5].split("-")
    if (set(info) != {"deleted_at", "subject_hash", "version"} or info["version"] != 1
            or subject != info["subject_hash"] or stamp.utcoffset() != timedelta(0)):
        raise ValueError("Deletion receipt identity mismatch")
    elapsed = stamp - datetime(1970, 1, 1, tzinfo=UTC)
    if timedelta(microseconds=int(micro)) != elapsed:
        raise ValueError("Deletion receipt time mismatch")


def mirror(path):
    data = path.read_bytes()
    check_receipt(path.name, data)
    digest = sha256(data)
    put_once(s3(), f"erasures/{digest.hexdigest()}/{path.name}", data,
             base64.b64encode(digest.digest()).decode(), idempotent=True)


def mongo(file):
    from pymongo import MongoClient

    # Production never accepts invalid certificates or cleartext connections.
    options = {"tlsCAFile": os.environ["REELDUB_BACKUP_CA"]} if os.getenv("REELDUB_BACKUP_CA") else {}
    return MongoClient(file.read_text().strip(), tls=True, tlsAllowInvalidCertificates=False,
                       tlsAllowInvalidHostnames=False, tz_aware=True,
                       serverSelectionTimeoutMS=10000, **options)


def file_checksum(file):
    digest = sha256()
    for block in iter(lambda: file.read(1024 * 1024), b""):
        digest.update(block)
    return base64.b64encode(digest.digest()).decode()


def save(uri_file):
    from snapshot import capture, verify_snapshot

    status = Path(os.environ["STATE_DIRECTORY"]) / "status.json"
    previous = json.loads(status.read_text()) if status.exists() else {}
    with mongo(uri_file) as client, tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "snapshot.zip"
        started_at = datetime.now(UTC)
        check_day = started_at.date().isoformat()
        check_source = previous.get("source_counts_checked_on") != check_day
        manifest = capture(client[os.environ["REELDUB_BACKUP_DATABASE"]], path,
                           started_at=started_at, verify_source_counts=check_source)
        verify_snapshot(path, manifest)
        category = "daily" if previous.get("last_daily") != check_day else "quarter-hour"
        key = f"snapshots/{category}/{started_at:%Y-%m-%dT%H-%M-%SZ}-{uuid4().hex}.zip"
        with path.open("rb") as stream:
            checksum = file_checksum(stream)
            stream.seek(0)
            put_once(s3(), key, stream, checksum)
        report = {"key": key, "started_at": manifest["started_at"], "last_daily": check_day,
                  "source_counts_checked_on": check_day,
                  "bytes": path.stat().st_size, "verified_collections": len(manifest["collections"]),
                  "verified_documents": sum(entry["count"] for entry in manifest["collections"].values())}
        pending = status.with_suffix(".tmp")
        pending.write_text(json.dumps(report))
        pending.replace(status)
        print(json.dumps(report))


def read_verified(client, key, destination):
    response = client.get_object(**bucket_args(), Key=key, ChecksumMode="ENABLED")
    digest = sha256()
    with closing(response["Body"]) as stream:
        for chunk in stream.iter_chunks(chunk_size=1024 * 1024):
            digest.update(chunk)
            destination.write(chunk)
    if (response.get("ServerSideEncryption") != "AES256" or
            response.get("ChecksumSHA256") != base64.b64encode(digest.digest()).decode()):
        raise ValueError("Cloud object's checksum or encryption policy failed validation")
    return digest.hexdigest()


def fetch_verified(key, temporary):
    from io import BytesIO

    if not SNAPSHOT.fullmatch(key):
        raise ValueError("Select a snapshot under the metadata prefix")
    cloud = s3()
    with (temporary / "coverage.json").open("xb") as proof:
        read_verified(cloud, COVERAGE, proof)
    with (temporary / "snapshot.zip").open("xb") as snapshot:
        read_verified(cloud, key, snapshot)
    journal = temporary / "receipts"
    journal.mkdir(mode=0o700)
    for page in cloud.get_paginator("list_objects_v2").paginate(**bucket_args(), Prefix="erasures/"):
        for item in page.get("Contents", []):
            prefix, hash_key, filename = item["Key"].split("/")
            if prefix != "erasures" or not RECEIPT.fullmatch(filename) or item["Size"] > 1024:
                raise ValueError("Malformed object in deletion journal")
            data = BytesIO()
            if read_verified(cloud, item["Key"], data) != hash_key:
                raise ValueError("Erasure checksum differs from its key")
            check_receipt(filename, data.getvalue())
            with (journal / filename).open("xb") as output:
                output.write(data.getvalue())
    (temporary / "export.json").write_text(json.dumps({
        "key": key, "receipt_count": len(list(journal.iterdir())), "fetched": datetime.now(UTC).isoformat(),
    }))


def restore_directory(uri_file, temporary, target):
    from snapshot import restore_isolated
    from server.db import mongo_indexes
    from server.erasure_replay import replay

    start = datetime.fromisoformat(json.loads((temporary / "coverage.json").read_text())["strict_from"])
    info = json.loads((temporary / "export.json").read_text())
    journal = temporary / "receipts"
    snapshot = temporary / "snapshot.zip"
    files = list(journal.iterdir())
    if not SNAPSHOT.fullmatch(info["key"]) or len(files) != info["receipt_count"]:
        raise ValueError("Incomplete recovery package")
    for path in files:
        check_receipt(path.name, path.read_bytes())
    with ZipFile(snapshot) as archive:
        stamp = datetime.fromisoformat(json.loads(archive.read("manifest.json"))["started_at"])
    if stamp < start or not datetime.now(UTC) - timedelta(days=7) <= stamp <= datetime.now(UTC):
        raise ValueError("Snapshot is outside verified deletion coverage; keep staging offline")
    with mongo(uri_file) as client:
        database = client[target]
        manifest = restore_isolated(database, snapshot)
        erased = replay(database, journal, stamp)
        mongo_indexes(database)
    print(json.dumps({"verified_staging_database": target, "snapshot": info["key"],
                      "started_at": manifest["started_at"], "receipts_scanned": len(files),
                      "accounts_erased": erased, "verified_collections": len(manifest["collections"]),
                      "verified_documents": sum(row["count"] for row in manifest["collections"].values())}))


def export_encrypted(key, certificate, output):
    # CloudShell can give an operator a recoverable archive without exporting its Console credentials.
    if output.exists():
        raise ValueError("Choose a new output file")
    with tempfile.TemporaryDirectory() as folder:
        temporary = Path(folder)
        fetch_verified(key, temporary)
        with tempfile.TemporaryFile() as bundle:
            with ZipFile(bundle, "w") as archive:
                for path in sorted(temporary.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(temporary))
            bundle.seek(0)
            with output.open("xb") as encrypted:
                subprocess.run(["openssl", "cms", "-encrypt", "-binary", "-aes256", "-outform", "DER",
                                str(certificate)], stdin=bundle, stdout=encrypted, check=True)


def unpack_verified(bundle, temporary):
    # Accept only the known package layout, with no symlinks, duplicate paths or path traversal.
    import shutil
    fixed = {"coverage.json", "export.json", "snapshot.zip"}
    (temporary / "receipts").mkdir(mode=0o700)
    with ZipFile(bundle) as archive:
        if not fixed.issubset(archive.namelist()):
            raise ValueError("Incomplete recovery package")
        for name in archive.namelist():
            parts = name.split("/")
            if name not in fixed and not (len(parts) == 2 and parts[0] == "receipts" and RECEIPT.fullmatch(parts[1])):
                raise ValueError("Unexpected file in recovery package")
            with archive.open(name) as source, (temporary / name).open("xb") as destination:
                shutil.copyfileobj(source, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="task", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--mongo-uri-file", required=True, type=Path)
    erasure = sub.add_parser("mirror")
    erasure.add_argument("receipt", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("--mongo-uri-file", required=True, type=Path)
    restore.add_argument("--key", required=True)
    restore.add_argument("--target", required=True)
    exporter = sub.add_parser("export-encrypted", help="Export from authenticated CloudShell for an offline recovery")
    exporter.add_argument("--key", required=True)
    exporter.add_argument("--certificate", type=Path, required=True)
    exporter.add_argument("--output", type=Path, required=True)
    staged = sub.add_parser("restore-export", help="Restore an authenticated operator's decrypted recovery package")
    staged.add_argument("--bundle", required=True, type=Path)
    staged.add_argument("--mongo-uri-file", required=True, type=Path)
    staged.add_argument("--target", required=True)
    # Run only after the API has required synchronous mirroring for every accepted deletion.
    sub.add_parser("start-coverage")
    status = sub.add_parser("status")
    status.add_argument("--file", type=Path, default=Path("/var/lib/reeldub-backup/status.json"))
    args = parser.parse_args()
    if args.task == "backup":
        save(args.mongo_uri_file)
    elif args.task == "mirror":
        mirror(args.receipt)
    elif args.task == "restore":
        with tempfile.TemporaryDirectory() as folder:
            fetch_verified(args.key, Path(folder))
            restore_directory(args.mongo_uri_file, Path(folder), args.target)
    elif args.task == "export-encrypted":
        export_encrypted(args.key, args.certificate, args.output)
    elif args.task == "restore-export":
        with tempfile.TemporaryDirectory() as folder:
            unpack_verified(args.bundle, Path(folder))
            restore_directory(args.mongo_uri_file, Path(folder), args.target)
    elif args.task == "status":
        info = json.loads(args.file.read_text())
        age = (datetime.now(UTC) - datetime.fromisoformat(info["started_at"])).total_seconds()
        print(json.dumps({**info, "age_seconds": int(age), "fresh": 0 <= age < 1800}))
        if not 0 <= age < 1800:
            raise SystemExit(1)
    else:
        data = json.dumps({"strict_from": datetime.now(UTC).isoformat()}).encode()
        put_once(s3(), COVERAGE, data, base64.b64encode(sha256(data).digest()).decode())


if __name__ == "__main__":
    main()
