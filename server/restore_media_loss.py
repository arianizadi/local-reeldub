"""Prepare isolated metadata staging when the entire private media cache was lost."""
import argparse
import json
import os
from pathlib import Path
import re

from pymongo import MongoClient
from pymongo.write_concern import WriteConcern

from .db import now
from .source import reel_url

FENCE_FIELDS = ("lease", "lease_end", "fleet_slot", "fleet_request", "upload_lease", "upload_token",
                "upload_until", "staged_source", "staged_lease", "attempt_started")
FUNDING_FIELDS = ("slot", "extra_ticket", "skip_ticket", "minute_units", "minute_grants", "render_key")


def preflight(database, *, apply=False):
    if not re.fullmatch(r"reeldub_restore_[a-z0-9_]{5,55}", database.name):
        raise ValueError("Expected an offline reeldub_restore_* staging database")
    if not {"accounts", "jobs", "sources", "quotes"}.issubset(database.list_collection_names()):
        raise ValueError("Restore and verify a complete metadata snapshot first")
    stamp = now()
    lost = {"$or": [{"artifact": {"$exists": True}}, {"has_poster": True},
                    {"state": "done", "media_expired": {"$ne": True}, "deleted": {"$ne": True}}]}
    sources = {"$or": [{"ready": True}, {"slot": {"$exists": True}}, {"retiring": {"$ne": True}}]}
    quotes = {"expires": {"$gt": stamp}}
    result = {"outputs_to_expire": database.jobs.count_documents(lost), "sources_to_expire": database.sources.count_documents(sources),
              "quotes_to_expire": database.quotes.count_documents(quotes), "requeue_reels": 0, "release_unrecoverable": 0}
    if apply:
        database.jobs.update_many(lost, {"$set": {"has_poster": False, "media_expired": True},
                                         "$unset": dict.fromkeys(("artifact", "poster", "media_expires", "awaiting_delivery", "render_key"), "")})
        database.sources.update_many(sources, {"$set": {"ready": False, "retiring": True, "expires": stamp},
                                              "$unset": {"slot": ""}})
        database.quotes.update_many(quotes, {"$set": {"expires": stamp}})
    active = {"state": {"$in": ["queued", "running"]}}
    for row in database.jobs.find(active, {k: 1 for k in ("url", "source_file", "quoted_seconds", "deleted", "state", *FENCE_FIELDS)}):
        try:
            downloadable = reel_url(row.get("url") or "") == row.get("url")
        except ValueError:
            downloadable = False
        pinned = row.get("source_file") is not None or row.get("quoted_seconds") is not None
        removed = dict.fromkeys(FENCE_FIELDS, "")
        if downloadable and not pinned and not row.get("deleted"):
            if row["state"] == "queued" and not any(key in row for key in FENCE_FIELDS):
                continue
            result["requeue_reels"] += 1
            changed = {"state": "queued", "stage": "Recovering render", "recovery_rank": 1,
                       "progress": 0, "estimated_finish": None, "recovery_reason": "storage_recovery"}
        else:
            result["release_unrecoverable"] += 1
            removed.update(dict.fromkeys(FUNDING_FIELDS, ""))
            changed = {"state": "failed", "stage": "Failed", "finished": stamp, "estimated_finish": None,
                       "error": "The saved source was lost during recovery. Submit the video again for a new quote."}
        if apply:
            database.jobs.update_one({"_id": row["_id"], "state": row["state"]}, {"$set": changed, "$unset": removed})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-uri-file", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--apply-absent-media", action="store_true", help="Confirm GridFS was entirely lost and apply changes")
    args = parser.parse_args()
    opts = {}
    if ca := os.getenv("REELDUB_BACKUP_CA"):
        opts["tlsCAFile"] = ca
    with MongoClient(args.mongo_uri_file.read_text().strip(), tls=True, tlsAllowInvalidCertificates=False,
                     tlsAllowInvalidHostnames=False, tz_aware=True, serverSelectionTimeoutMS=10000, **opts) as client:
        target = client.get_database(args.target, write_concern=WriteConcern(w="majority", j=True, wtimeout=10000))
        print(json.dumps(preflight(target, apply=args.apply_absent_media)))


if __name__ == "__main__":
    main()
