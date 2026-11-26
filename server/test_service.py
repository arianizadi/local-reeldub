"""Integration checks. Run only in the disposable Compose project described in PRODUCT.md."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import secrets
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch
from uuid import uuid4

from .api import hash_value
from .db import media_db, db, now
from .worker import LEASE, RenderLease, claim, quality_options


class WorkerLeaseTests(unittest.TestCase):


    def test_light_worker_only_claims_jobs_it_can_render(self):
        queue = Mock()
        with patch("server.worker.uses_postgres", return_value=False), patch("server.worker.db", return_value=queue), patch("server.worker.Path.is_file", return_value=False):
            claim()
        query = queue.jobs.find_one_and_update.call_args.args[0]
        self.assertIsNone(query["render_quality"])
        self.assertIsNone(query["lab_mode"])
        with patch("server.worker.uses_postgres", return_value=False), patch("server.worker.db", return_value=queue), patch("server.worker.Path.is_file", return_value=True):
            claim()
        self.assertNotIn("render_quality",queue.jobs.find_one_and_update.call_args.args[0])

    def test_progress_renews_fenced_lease_and_quiet_stage_still_heartbeats(self):
        jobs = Mock()
        jobs.update_one.return_value.matched_count = 1
        fence = {"_id": "test", "state": "running", "lease": "attempt-1"}
        stamp = now()
        with patch("server.worker.time.monotonic", return_value=0) as clock, patch("server.worker.now", return_value=stamp):
            lease = RenderLease(jobs, fence, stamp + LEASE)
            clock.return_value = 12
            self.assertTrue(lease.renew({"stage": "Voicing"}))
            clock.return_value = 15
            self.assertTrue(lease.renew())
            jobs.update_one.assert_called_once_with({**fence, "lease_end": {"$gt": stamp}},
                                                   {"$set": {"stage": "Voicing", "lease_end": stamp + LEASE}})
            clock.return_value = 30
            self.assertTrue(lease.renew())
            self.assertEqual(jobs.update_one.call_count, 2)
            jobs.update_one.return_value.matched_count = 0
            self.assertFalse(lease.renew({"progress": 0.70}))


@unittest.skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1", "Requires a disposable queue database")
class QueueIntegration(unittest.TestCase):

    def test_recover_interrupted_video_deletion(self):
        import gridfs
        from .api import resume_deletions

        bucket = gridfs.GridFS(media_db())
        artifact = bucket.put(b"disposable")
        job = {"_id": str(uuid4()), "owner": "test:" + str(uuid4()), "request_id": str(uuid4()),
               "state": "done", "deleted": True, "artifact": artifact}
        db().jobs.insert_one(job)
        resume_deletions()
        resume_deletions()
        self.assertFalse(bucket.exists(artifact))
        self.assertNotIn("artifact", db().jobs.find_one({"_id": job["_id"]}))


    def test_video_tickets_and_ranges(self):
        import gridfs
        token = os.environ["SELF_HOST_KEY"]
        owner = "self-host"
        job = str(uuid4())
        data = b"fourteen bytes"
        artifact = gridfs.GridFS(media_db()).put(data, filename=job)
        db().jobs.insert_one({"_id": job, "owner": owner, "state": "done", "artifact": artifact})
        base = "http://127.0.0.1:8300/v1/"
        req = Request(base + "jobs/" + job + "/playback", data=b"", headers={"Authorization": "Bearer " + token})
        with urlopen(req) as response:
            ticket = json.load(response)["ticket"]
        url = base + "play/" + job + "/" + ticket
        for r, expected in (("bytes=0-3", data[:4]), ("bytes=-3", data[-3:]), ("bytes=5-", data[5:])):
            with urlopen(Request(url, headers={"Range": r})) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), expected)
        with urlopen(Request(url, method="HEAD")) as response:
            self.assertEqual(response.headers["Content-Length"], str(len(data)))
        for target, headers, code in ((url, {"Range":"bytes=99-"}, 416), (url + "x", {}, 404),
                                      (base + "play/other/" + ticket, {}, 404)):
            with self.assertRaises(HTTPError) as err:
                urlopen(Request(target, headers=headers))
            self.assertEqual(err.exception.code, code)
