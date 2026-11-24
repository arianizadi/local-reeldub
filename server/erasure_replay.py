"""Apply complete deletion receipts to an offline, separately named metadata restore."""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pymongo import MongoClient

from .erasure import erase_data
from .erasure_journal import read_receipt, subject_hash


def replay(database, journal_directory, snapshot_started_at):
    if not database.name.startswith("reeldub_restore_"):
        raise ValueError("Expected an isolated reeldub_restore_* database")
    now = datetime.now(timezone.utc)
    if (snapshot_started_at.tzinfo is None or snapshot_started_at > now
            or snapshot_started_at < now - timedelta(days=7)):
        raise ValueError("Snapshot must be within the seven-day recovery window")
    folder = Path(journal_directory)
    if not folder.is_dir():
        raise ValueError("Missing independent account deletion journal")
    receipts = {}
    for path in folder.glob("*.json"):
        stamp, digest = read_receipt(path)
        receipts[digest] = max(stamp, receipts.get(digest, stamp))
    # A crash between removing the account and all associated rows can leave owner-only data.
    subjects = {}
    collections = {"accounts": "_id", "account_deletions": "_id", "sessions": "owner",
                   "playback": "owner", "purchase_intents": "owner", "purchases": "owner",
                   "jobs": "owner", "quotes": "owner", "sources": "owner", "beta_allowances": "owner"}
    for name, field in collections.items():
        for row in database[name].find({field: {"$regex": "^apple:"}}, {field: 1}):
            owner = row[field]
            stamp = receipts.get(subject_hash(owner))
            if stamp:
                subjects[owner] = stamp
    erased = 0
    for user, deleted in subjects.items():
        account = database.accounts.find_one({"_id": user}, {"created": 1}) or {}
        created = account.get("created")
        # Re-created accounts have a distinct library; a previous erasure cannot touch it.
        if created and created.replace(tzinfo=timezone.utc) > deleted:
            continue
        erase_data(database, user)
        until = max(deleted + timedelta(minutes=15),
                    deleted.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
        if until > now:
            database.account_deletions.update_one({"_id": user}, {"$set": {
                "created": deleted, "expires": until}}, upsert=True)
        else:
            database.account_deletions.delete_one({"_id": user})
        erased += 1
    return erased


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri-file", required=True, type=Path)
    parser.add_argument("--database", required=True)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--snapshot-start", required=True, type=datetime.fromisoformat)
    parser.add_argument("--tls-ca")
    args = parser.parse_args()
    options = {"tls": True, "tlsCAFile": args.tls_ca} if args.tls_ca else {}
    with MongoClient(args.uri_file.read_text().strip(), tz_aware=True, **options) as client:
        count = replay(client[args.database], args.journal, args.snapshot_start)
    print(f"Deletion reconciliation finished for {count} restored account(s).")


if __name__ == "__main__":
    main()
