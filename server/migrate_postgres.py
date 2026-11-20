"""Import ReelDub metadata with both API writers and all queue workers stopped."""

import argparse
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .db import media_db
from .postgres import TABLES, encode, install


def checksum(rows):
    h = hashlib.sha256()
    for item in sorted(
        json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows
    ):
        h.update(item.encode())
        h.update(b"\n")
    return h.digest()


def migrate(conn):
    source = media_db()
    ignored = {"fs.files", "fs.chunks", "phone_rates", "phone_challenges", "queue_wake"}
    unhandled = set(source.list_collection_names()) - set(TABLES) - ignored
    if unhandled:
        raise RuntimeError("Unrecognized source collections; review migration mapping")
    if source.jobs.count_documents({"state": {"$in": ["running", "queued"]}}):
        raise RuntimeError("Drain active jobs before migration")
    for name in TABLES:
        docs = []
        for row in source[name].find({}):
            if name == "accounts":
                row.pop("phone_hash", None)
            if name == "jobs":
                poster = row.pop("poster", None)
                if artifact := row.get("artifact"):
                    cache = source["fs.files"]
                    if not cache.find_one({"_id": artifact}, {"_id": 1}):
                        raise RuntimeError(
                            "Referenced video missing from the private cache"
                        )
                    if poster:
                        cache.update_one(
                            {"_id": artifact}, {"$set": {"poster": poster}}
                        )
            docs.append(encode(row))
        table = sql.Identifier("reeldub", name)
        with conn.cursor() as cursor:
            cursor.executemany(
                sql.SQL(
                    "INSERT INTO {}(doc) VALUES(%s) ON CONFLICT(id) DO NOTHING"
                ).format(table),
                [(Jsonb(doc),) for doc in docs],
            )
        copied = [
            r[0] for r in conn.execute(sql.SQL("SELECT doc FROM {}").format(table))
        ]
        if checksum(copied) != checksum(docs):
            raise RuntimeError(
                f"{name}: destination differs from source; aborting transaction"
            )
        print(f"{name}: verified {len(docs)} records")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metadata",
        action="store_true",
        help="Import and compare metadata after draining all writers.",
    )
    args = parser.parse_args()
    with psycopg.connect(
        Path(os.environ["REELDUB_MIGRATION_DSN_FILE"]).read_text().strip(),
        sslmode="verify-full",
        sslrootcert=os.getenv("PGSSLROOTCERT", "/etc/ssl/certs/ca-certificates.crt"),
    ) as conn:
        install(conn)
        if args.metadata:
            migrate(conn)
