"""Exercise recovery against an ephemeral MongoDB staging namespace only."""
import os
import unittest
from datetime import timedelta
from uuid import uuid4

from pymongo import MongoClient

from .db import now
from .restore_media_loss import preflight


@unittest.skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1", "Requires a disposable Mongo database")
class MediaLossTests(unittest.TestCase):
    def test_idempotent_recovery_preserves_completed_spending_and_fences_active_jobs(self):
        client = MongoClient(os.environ["MONGO_URI"], tz_aware=True)
        self.addCleanup(client.close)
        db = client["reeldub_restore_" + uuid4().hex]
        self.addCleanup(client.drop_database, db.name)
        for name in ("accounts", "sources", "jobs", "quotes"):
            db.create_collection(name)
        link = "https://www.instagram.com/reel/Sample/"
        db.sources.insert_one({"_id": "src", "ready": True, "slot": "source"})
        db.quotes.insert_one({"expires": now() + timedelta(hours=1)})
        db.jobs.insert_many([
            {"_id": "completed", "state": "done", "artifact": "gone", "has_poster": True, "minute_units": ["credit"]},
            {"_id": "queued", "state": "queued", "url": link, "slot": 0},
            {"_id": "direct", "state": "running", "url": link, "lease": "old", "fleet_slot": "a:0", "slot": 1},
            {"_id": "upload", "state": "running", "url": "upload:fixture", "source_file": "src", "lease": "old",
             "minute_units": ["unit"], "minute_grants": ["grant"]},
            {"_id": "quoted", "state": "queued", "url": link, "quoted_seconds": 5, "slot": 2},
        ])
        expected = {"outputs_to_expire": 1, "sources_to_expire": 1, "quotes_to_expire": 1,
                    "requeue_reels": 1, "release_unrecoverable": 2}
        self.assertEqual(preflight(db), expected)
        self.assertEqual(db.jobs.find_one({"_id": "upload"})["lease"], "old")
        self.assertEqual(preflight(db, apply=True), expected)
        self.assertFalse(any(preflight(db, apply=True).values()))
        done = db.jobs.find_one({"_id": "completed"})
        self.assertEqual(done["minute_units"], ["credit"])
        self.assertTrue(done["media_expired"])
        self.assertNotIn("artifact", done)
        direct = db.jobs.find_one({"_id": "direct"})
        self.assertEqual((direct["state"], direct["recovery_rank"], direct["slot"]), ("queued", 1, 1))
        self.assertNotIn("lease", direct)
        self.assertNotIn("fleet_slot", direct)
        upload = db.jobs.find_one({"_id": "upload"})
        self.assertEqual(upload["state"], "failed")
        self.assertNotIn("minute_units", upload)
        self.assertNotIn("slot", db.jobs.find_one({"_id": "quoted"}))
        self.assertNotIn("recovery_rank", db.jobs.find_one({"_id": "queued"}))
        with self.assertRaises(ValueError):
            preflight(client.reeldub_metadata, apply=True)
