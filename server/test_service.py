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

    def test_verified_apple_session_is_single_use(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from fastapi import HTTPException
        from .api import AppleLogin, apple, apple_keys

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        challenge = secrets.token_urlsafe(32)
        nonce = hash_value(challenge)
        stamp = now()
        db().challenges.insert_one({"_id": nonce, "expires": stamp + timedelta(minutes=2)})
        subject = str(uuid4())
        claims = dict(iss="https://appleid.apple.com", sub=subject, aud="reeldub.tests",
                      exp=stamp + timedelta(minutes=2), iat=stamp, nonce=nonce)

        def exchange(values):
            identity = jwt.encode(values, private_key, algorithm="RS256")
            return apple(AppleLogin(identity_token=identity, challenge=challenge))

        with patch.dict(os.environ, {"APPLE_CLIENT_ID": "reeldub.tests", "ADMIN_APPLE_EMAIL": "owner@example.org"}), patch.object(
                apple_keys, "get_signing_key_from_jwt", return_value=SimpleNamespace(key=private_key.public_key())):
            for invalid in ({"aud": "another-app"}, {"nonce": "unmatched"}, {"iss": "another-issuer"},
                            {"exp": stamp - timedelta(minutes=1)}):
                with self.assertRaises(HTTPException) as rejected:
                    exchange({**claims, **invalid})
                self.assertEqual(rejected.exception.status_code, 401)
            token = exchange({**claims, "email": "owner@example.org", "email_verified": "false"})["token"]
            self.assertNotEqual(db().accounts.find_one({"_id": "apple:" + subject}).get("tier"), "admin")
            self.assertEqual(db().sessions.find_one({"_id": hash_value(token)})["owner"], "apple:" + subject)
            with self.assertRaises(HTTPException) as replay:
                exchange(claims)
            self.assertEqual(replay.exception.status_code, 401)
            db().challenges.insert_one({"_id": nonce, "expires": stamp + timedelta(minutes=2)})
            exchange({**claims, "email": "OWNER@example.org", "email_verified": True})
            self.assertEqual(db().accounts.find_one({"_id": "apple:" + subject})["tier"], "admin")

    def test_video_tickets_and_ranges(self):
        import gridfs
        token = secrets.token_urlsafe(36)
        owner = "apple:" + str(uuid4())
        job = str(uuid4())
        data = b"fourteen bytes"
        artifact = gridfs.GridFS(db()).put(data, filename=job)
        db().jobs.insert_one({"_id": job, "owner": owner, "state": "done", "artifact": artifact})
        db().sessions.insert_one({"_id": hash_value(token), "owner": owner, "expires": now() + timedelta(minutes=2)})
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
