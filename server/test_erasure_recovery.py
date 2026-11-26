"""Restore and account-deletion regression tests; only use a disposable database."""
from datetime import timedelta
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import gridfs
from fastapi import HTTPException

from . import api, erasure, erasure_journal, erasure_replay
from .db import db, media_db, now


class JournalTests(unittest.TestCase):


    def test_private_idempotent_receipt_and_aged_pruning(self):
        stamp = now()
        with TemporaryDirectory() as journal, patch.dict(os.environ, {
            "REELDUB_ERASURE_JOURNAL_DIR": journal, "REELDUB_ERASURE_MIRROR_COMMAND": ""
        }):
            for _ in range(2):
                erasure_journal.record("apple:private-test-subject", stamp)
            path, = Path(journal).glob("*.json")
            self.assertNotIn("private-test-subject", path.read_text())
            self.assertEqual(erasure_journal.read_receipt(path), (
                stamp, erasure_journal.subject_hash("apple:private-test-subject")))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            erasure_journal.prune(stamp + timedelta(days=16))
            self.assertFalse(path.exists())


@unittest.skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1", "Requires disposable MongoDB")
class RestoreTests(unittest.TestCase):
    def test_stale_snapshot_scrub_preserves_recreated_accounts_and_isolates_media(self):
        target = db().client["reeldub_restore_" + uuid4().hex]
        self.addCleanup(db().client.drop_database, target.name)
        stamp = now()
        old = "apple:deleted"
        newer = "apple:recreated"
        orphan = "apple:orphan"
        keep = "apple:someone-else"
        for owner in (old, newer, keep):
            created = stamp + timedelta(hours=1) if owner == newer else stamp - timedelta(days=2)
            target.accounts.insert_one({"_id": owner, "created": created, "email": "private@example.test"})
        bucket = gridfs.GridFS(media_db())
        blob = bucket.put(b"a different live database")
        self.addCleanup(bucket.delete, blob)
        for owner in (old, orphan, newer, keep):
            target.jobs.insert_one({"owner": owner, "url": "private", "artifact": blob, "state": "done"})
        with TemporaryDirectory() as journal, patch.dict(os.environ, {
            "REELDUB_ERASURE_JOURNAL_DIR": journal, "REELDUB_ERASURE_MIRROR_COMMAND": ""
        }):
            for owner in (old, newer, orphan):
                erasure_journal.record(owner, stamp)
            for _ in range(2):
                erasure_replay.replay(target, journal, stamp - timedelta(days=1))
            with self.assertRaises(ValueError):
                erasure_replay.replay(db(), journal, stamp)
        self.assertIsNone(target.accounts.find_one({"_id": old}))
        self.assertEqual(set(target.jobs.distinct("owner")), {newer, keep})
        self.assertIsNotNone(target.accounts.find_one({"_id": newer}))
        self.assertTrue(bucket.exists(blob))

    def test_delayed_library_removal_preserves_newer_cached_request(self):
        user = "apple:" + uuid4().hex
        job = str(uuid4())
        stamp = now().replace(microsecond=0)
        blob = gridfs.GridFS(media_db()).put(b"cached video")
        self.addCleanup(gridfs.GridFS(media_db()).delete, blob)
        db().jobs.insert_one({"_id": job, "request_id": job, "owner": user, "state": "done",
                              "artifact": blob, "media_expires": now() + timedelta(hours=1),
                              "last_requested": stamp})
        self.addCleanup(db().jobs.delete_one, {"_id": job})
        for stale in ("original", (stamp - timedelta(hours=1)).isoformat()):
            with self.assertRaises(HTTPException) as rejected:
                api.delete_dub(job, user, stale)
            self.assertEqual(rejected.exception.status_code, 412)
            self.assertFalse(db().jobs.find_one({"_id": job}).get("deleted"))
        api.delete_dub(job, user, stamp.isoformat())
        self.assertTrue(db().jobs.find_one({"_id": job})["cache_retained"])
        self.assertTrue(gridfs.GridFS(media_db()).exists(blob))
