"""Copy ReelDub's PostgreSQL metadata to a new dedicated Mongo database.

Run with all metadata writers stopped. The source is read-only. Existing,
identical rows are accepted for interrupted imports; destination conflicts
are never overwritten. GridFS and the private coordinator cache stay in place.
"""
import argparse
import hashlib
import json
import os
from itertools import islice
from pathlib import Path

import psycopg
from psycopg import sql
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError

from .db import metadata_mongo, mongo_indexes
from .postgres import TABLES, decode, encode


def canonical(row):
    return json.dumps(row, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()


def verify_key_tables(conn):
    # These two relational indexes are represented by Mongo unique multikey
    # indexes over the same arrays on jobs; they must have no independent data.
    for table, field, columns, projected in (
        ("spend_keys", "minute_units", "unit, job", "u, j.id"),
        ("request_keys", "request_keys", "owner, request, job", "j.doc->>'owner', u, j.id"),
    ):
        query = sql.SQL("""
            WITH stored AS (SELECT {cols} FROM reeldub.{table}), expected AS
             (SELECT DISTINCT {projection} FROM reeldub.jobs j CROSS JOIN LATERAL
              jsonb_array_elements_text(j.doc->%s) AS u)
            SELECT EXISTS((TABLE stored EXCEPT TABLE expected) UNION ALL
                          (TABLE expected EXCEPT TABLE stored))
        """).format(cols=sql.SQL(columns), table=sql.Identifier(table), projection=sql.SQL(projected))
        if conn.execute(query, (field,)).fetchone()[0]:
            raise RuntimeError(f"{table}: ledger does not match job records")


def migrate(conn, target, *, verify_only=False):
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    tables = {row[0] for row in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='reeldub'")}
    if tables != set(TABLES) | {"request_keys", "spend_keys"}:
        raise RuntimeError("Source table inventory differs from the expected ReelDub schema")
    if set(target.list_collection_names()) - set(TABLES) - {"fleet_claim_locks"}:
        raise RuntimeError("Use a dedicated destination containing only ReelDub metadata")
    if conn.execute("""SELECT EXISTS(SELECT 1 FROM reeldub.jobs WHERE doc->>'state'='running')""").fetchone()[0]:
        raise RuntimeError("Drain active renders before copying or verifying the frozen source")
    verify_key_tables(conn)
    summary = {}
    for name in TABLES:
        collection = target[name]
        count, digest = 0, hashlib.sha256()
        with conn.cursor(name="transfer_" + name) as cursor:
            cursor.execute(sql.SQL("SELECT doc FROM {} ORDER BY id").format(sql.Identifier("reeldub", name)))
            while batch := list(islice(cursor, 128)):
                docs = [decode(raw) for raw, in batch]
                if not verify_only:
                    try:
                        collection.bulk_write([UpdateOne({"_id": doc["_id"]}, {"$setOnInsert": doc}, upsert=True)
                                               for doc in docs])
                    except BulkWriteError:
                        # Avoid emitting raw documents or personal or financial identifiers.
                        raise RuntimeError(f"{name}: destination rejected a source row") from None
                stored = {canonical(encode(row["_id"])): canonical(encode(row))
                          for row in collection.find({"_id": {"$in": [doc["_id"] for doc in docs]}})}
                for raw, in batch:
                    expected = canonical(raw)
                    if stored.get(canonical(raw["_id"])) != expected:
                        raise RuntimeError(f"{name}: destination differs from frozen source")
                    digest.update(expected + b"\n")
                    count += 1
        if count != collection.count_documents({}):
            raise RuntimeError(f"{name}: unexpected destination records")
        summary[name] = {"count": count, "sha256": digest.hexdigest()}
        print(f"{name}: {count} verified, sha256={digest.hexdigest()}", flush=True)
    if not verify_only:
        # Deferred until verification: TTL workers may legitimately remove expired rows.
        mongo_indexes(target)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--copy", action="store_true", help="Copy into an unused database and verify every record")
    mode.add_argument("--verify-only", action="store_true", help="Compare all records against a frozen source")
    args = parser.parse_args()
    dsn = Path(os.environ["REELDUB_MIGRATION_DSN_FILE"]).read_text().strip()
    options = {"connect_timeout": 10}
    if os.getenv("REELDUB_TEST_DATABASE") != "1":
        options.update(sslmode="verify-full", sslrootcert=os.getenv("PGSSLROOTCERT", "/etc/ssl/certs/ca-certificates.crt"))
    with psycopg.connect(dsn, **options) as conn:
        migrate(conn, metadata_mongo(), verify_only=args.verify_only)


if __name__ == "__main__":
    main()
