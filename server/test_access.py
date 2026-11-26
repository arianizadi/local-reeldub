"""Direct HTTP and origin authorization checks against a disposable hosted API."""
from datetime import timedelta
import json
import os
import secrets
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import HTTPException

from .api import hash_value, owner
from .db import media_db, db, now
from .media_network import allowed_host, connect_public
from .source import reel_url


class SourcePolicy(unittest.TestCase):
    def test_individual_reels_only(self):
        canonical = "https://www.instagram.com/reel/Ab_cd-123/"
        for prefix in ("https://instagram.com/reel/", "https://m.instagram.com/reels/", "https://www.instagram.com/reel/"):
            self.assertEqual(reel_url(prefix + "Ab_cd-123/?share=123"), canonical)
        for value in ("https://example.org/reel/Ab_cd-123/", "https://instagram.com.example.org/reel/Ab_cd-123/",
                      "https://instagram.com/p/Ab_cd-123/", "https://instagram.com/", "file:///example.mp4",
                      "https://example.org@instagram.com/reel/Ab_cd-123/", "https://instagram.com:444/reel/Ab_cd-123/",
                      "http://instagram.com/reel/Ab_cd-123/", "https://youtu.be/Ab_cd-123", "/example.mp4"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reel_url(value)
        self.assertTrue(allowed_host("video.cdninstagram.com"))
        self.assertFalse(allowed_host("instagram.com.example.org"))
        with patch("socket.getaddrinfo", return_value=[(2, 1, 0, "", ("127.0.0.1", 443))]), self.assertRaises(ValueError):
            connect_public("www.instagram.com")


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
