"""Round-trip the real BSON and SQL ledger formats using isolated test databases."""
import os
import unittest
from datetime import timedelta
from uuid import uuid4

from bson import ObjectId
import psycopg
from psycopg.types.json import Jsonb
from pymongo.errors import DuplicateKeyError

from .db import metadata_mongo, now
from .migrate_mongo import migrate
from .postgres import encode, install


@unittest.skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1" and os.getenv("REELDUB_TEST_MIGRATION_DSN"),
                     "Requires disposable PostgreSQL and MongoDB")
class MigrateMongoTests(unittest.TestCase):
    def test_verified_copy_resume_collision_and_unique_spending(self):
        dsn = os.environ["REELDUB_TEST_MIGRATION_DSN"]
        client = metadata_mongo().client
        target = client["reeldub_qa_" + uuid4().hex]
        self.addCleanup(client.drop_database, target.name)
        job_id, account = uuid4().hex, "apple:" + uuid4().hex
        artifact, unit, key = ObjectId(), uuid4().hex, uuid4().hex
        doc = {"_id": job_id, "owner": account, "request_id": key, "state": "done",
               "artifact": artifact, "finished": now() - timedelta(days=1), "priority": 0,
               "request_keys": [key], "minute_units": [unit]}
        with psycopg.connect(dsn) as conn:
            install(conn)
            conn.execute("INSERT INTO reeldub.jobs(doc) VALUES(%s)", (Jsonb(encode(doc)),))
        def clean_sql():
            with psycopg.connect(dsn) as conn:
                conn.execute("DELETE FROM reeldub.jobs WHERE id=%s", (Jsonb(job_id),))
        self.addCleanup(clean_sql)
        def run(verify=False):
            with psycopg.connect(dsn) as conn:
                return migrate(conn, target, verify_only=verify)
        first = run()
        self.assertEqual(run(), first)
        self.assertEqual(run(True), first)
        restored = target.jobs.find_one({"_id": job_id})
        self.assertEqual(encode(restored), encode(doc))
        for funding in ({"request_keys": [key]}, {"minute_units": [unit]}):
            with self.assertRaises(DuplicateKeyError):
                target.jobs.insert_one({"_id": uuid4().hex, "owner": account, "request_id": uuid4().hex, **funding})
        target.jobs.update_one({"_id": job_id}, {"$set": {"priority": 1}})
        with self.assertRaisesRegex(RuntimeError, "differs from frozen source"):
            run()
        self.assertEqual(target.jobs.find_one({"_id": job_id})["priority"], 1)
