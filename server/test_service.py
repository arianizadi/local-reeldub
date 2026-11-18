"""Integration checks. Run only in the disposable Compose project described in PRODUCT.md."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import secrets
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from .api import hash_value
from .db import db, now
from .worker import claim


@unittest.skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1", "Requires a disposable queue database")
class QueueIntegration(unittest.TestCase):
    def test_quotas_priority_leases_and_ownership(self):
        self.assertEqual(db().jobs.count_documents({}), 0, "Use an empty test database")
        def account(plan):
            token = secrets.token_urlsafe(36)
            user = "apple:" + str(uuid4())
            db().sessions.insert_one({"_id": hash_value(token), "owner": user, "expires": now() + timedelta(minutes=2)})
            db().accounts.insert_one({"_id": user, "tier": plan})
            return token
        def call(token, path, body=None):
            request = Request("http://127.0.0.1:8300" + path,
                              data=json.dumps(body).encode() if body is not None else None,
                              headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            try:
                with urlopen(request) as result:
                    return result.status, json.load(result)
            except HTTPError as error:
                return error.code, json.load(error)
        def submit(token):
            return call(token, "/v1/jobs", {"url": "https://www.instagram.com/reel/ExampleReel/", "request_id": str(uuid4())})
        free, paid, admin = [account(p) for p in ("free", "paid", "admin")]
        with ThreadPoolExecutor(max_workers=10) as pool:
            free_results = list(pool.map(submit, [free] * 14))
            paid_results = list(pool.map(submit, [paid] * 53))
        self.assertEqual(sum(s == 200 for s, _ in free_results), 10)
        self.assertEqual(sum(s == 200 for s, _ in paid_results), 50)
        job = claim()
        self.assertEqual(job["priority"], 1)
        db().jobs.update_one({"_id": job["_id"]}, {"$set": {"lease_end": now() - timedelta(seconds=1)}})
        retry = claim()
        self.assertEqual(retry["_id"], job["_id"])
        self.assertNotEqual(retry["lease"], job["lease"])
        self.assertEqual(db().jobs.update_one({"_id": job["_id"], "lease": job["lease"]},
                         {"$set": {"stage": "stale"}}).matched_count, 0)
        path = "/v1/jobs/" + job["_id"]
        self.assertEqual(call(free, path)[0], 404)
        self.assertEqual(call(free, path + "/video")[0], 404)
        self.assertEqual(call(free, path + "/report", {"category":"Timing","note":"A late line"})[0], 404)
        self.assertEqual(call(paid, path + "/report", {"category":"Timing","note":"A late line"})[0], 200)
        self.assertEqual(call("anonymous", "/v1/jobs")[0], 401)
        body = {"url": "https://www.instagram.com/reel/ExampleReel/?tracking=123", "request_id": str(uuid4())}
        self.assertEqual(call(admin, "/v1/jobs", body)[1]["id"], call(admin, "/v1/jobs", body)[1]["id"])
        self.assertIsNone(call(admin, "/v1/me")[1]["limit"])
        self.assertEqual(call(admin, "/v1/jobs", {**body,"url":"file:///private/file"})[0], 422)

