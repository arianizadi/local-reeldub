"""Direct HTTP and origin authorization checks against a disposable hosted API."""
from datetime import timedelta
import os
import unittest
from uuid import uuid4

from fastapi import HTTPException

from .db import media_db, db, now


@unittest.skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1", "Requires a disposable hosted API")
class HostedAccess(unittest.TestCase):
    def test_account_rate_limit_and_orphan_sweep(self):
        import gridfs
        from .api import limit_writes, purge_orphaned_uploads
        user = "apple:" + str(uuid4())
        limit_writes(user, "test", 1)
        with self.assertRaises(HTTPException) as rejected:
            limit_writes(user, "test", 1)
        self.assertEqual(rejected.exception.status_code, 429)
        bucket = gridfs.GridFS(media_db())
        running, published, gone = [str(uuid4()) for _ in range(3)]
        items = [bucket.put(b"sample", job=job, lease="lease") for job in (running, published, gone)]
        self.addCleanup(db().jobs.delete_many, {"owner": user})
        for item in items:
            self.addCleanup(bucket.delete, item)
        media_db()["fs.files"].update_many({"_id": {"$in": items}}, {"$set": {"uploadDate": now()-timedelta(hours=3)}})
        for fixture in [
            {"_id": running, "owner": user, "request_id": str(uuid4()), "state": "running", "lease": "lease"},
            {"_id": published, "owner": user, "request_id": str(uuid4()), "state": "done", "artifact": items[1]}]:
            db().jobs.insert_one(fixture)
        purge_orphaned_uploads()
        self.assertEqual([bucket.exists(i) for i in items], [True, True, False])
