"""Point-in-time BSON exports of ReelDub metadata and verified, isolated restores."""

from datetime import UTC, datetime
from hashlib import sha256
import json
import re
from zipfile import ZIP_DEFLATED, ZipFile

from bson import decode_file_iter
from bson.codec_options import CodecOptions
from bson.raw_bson import RawBSONDocument


# Short-lived session credentials and leases must never become valid on restore.
TRANSIENT = {"sessions", "playback", "challenges", "signin_challenges", "fleet_claim_locks", "queue_wake"}
COLLECTIONS = (
    "account_deletions", "accounts", "beta_allowances", "billing_bindings", "counters",
    "jobs", "purchase_intents", "purchases", "quotes", "request_rates", "sources",
    "store_history", "store_quarantine", "subscription_status",
)
RAW = CodecOptions(document_class=RawBSONDocument, tz_aware=True)
STAGING_NAME = re.compile(r"reeldub_restore_[a-z0-9_]{5,55}\Z")


def capture(database, path):
    names = set(database.list_collection_names())
    unknown = names - set(COLLECTIONS) - TRANSIENT
    if unknown:
        raise ValueError("Review unrecognized metadata collection(s): " + ", ".join(sorted(unknown)))
    if "accounts" not in names:
        raise ValueError("Missing accounts collection; refusing an empty/uninitialized source")
    manifest = {"format": 1, "started_at": datetime.now(UTC).isoformat(), "collections": {}}
    with database.client.start_session(snapshot=True) as session, ZipFile(path, "x", ZIP_DEFLATED) as archive:
        for name in COLLECTIONS:
            digest, count = sha256(), 0
            with archive.open(name + ".bson", "w", force_zip64=True) as output:
                projection = {"poster": 0} if name == "jobs" else None
                with database.get_collection(name, codec_options=RAW).find(
                    projection=projection, session=session
                ).sort("_id", 1) as rows:
                    for row in rows:
                        output.write(row.raw)
                        digest.update(row.raw)
                        count += 1
            manifest["collections"][name] = {"count": count, "sha256": digest.hexdigest()}
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
    return manifest


def restore_isolated(database, path):
    """Write only to a fresh staging namespace; verify every collection from Mongo itself."""
    if not STAGING_NAME.fullmatch(database.name) or database.list_collection_names():
        raise ValueError("Target must be an empty database named reeldub_restore_<unique suffix>")
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        entries = ["manifest.json", *(name + ".bson" for name in COLLECTIONS)]
        if (manifest.get("format") != 1 or set(manifest["collections"]) != set(COLLECTIONS)
                or sorted(archive.namelist()) != sorted(entries)):
            raise ValueError("Unsupported or incomplete archive")
        if datetime.fromisoformat(manifest["started_at"]).tzinfo is None:
            raise ValueError("Snapshot time lacks a timezone")
        for name in COLLECTIONS:
            target = database.create_collection(name)
            with archive.open(name + ".bson") as input_file:
                batch = []
                for doc in decode_file_iter(input_file, codec_options=RAW):
                    batch.append(doc)
                    if len(batch) == 100:
                        target.insert_many(batch)
                        batch.clear()
                if batch:
                    target.insert_many(batch)
            digest, count = sha256(), 0
            with target.with_options(codec_options=RAW).find().sort("_id", 1) as rows:
                for row in rows:
                    digest.update(row.raw)
                    count += 1
            if {"count": count, "sha256": digest.hexdigest()} != manifest["collections"][name]:
                raise ValueError("Restored document validation failed for " + name)
    return manifest
