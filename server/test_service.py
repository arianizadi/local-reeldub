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
    def test_new_best_jobs_use_preset_and_lab_runs_keep_their_own_options(self):
        self.assertEqual(quality_options({"render_quality":"best"}), ["--quality","best"])
        self.assertEqual(quality_options({}), [])
        experiment = {"lab_mode":"medium", "lab_voice":"small", "voice_batch":1, "lab_separation":"fp16"}
        self.assertEqual(quality_options(experiment), [
            "--quality","medium","--voice-batch","1","--lab-runtime","standard",
            "--lab-separation","fp16","--lab-voice","small"])

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
    def test_quotas_priority_leases_and_ownership(self):
        self.assertEqual(db().jobs.count_documents({}), 0, "Use an empty test database")
        def account(plan):
            token = secrets.token_urlsafe(36)
            user = "apple:" + str(uuid4())
            db().sessions.insert_one({"_id": hash_value(token), "owner": user, "expires": now() + timedelta(minutes=2)})
            db().accounts.insert_one({"_id": user, "tier": plan})
            return token
        def call(token, path, body=None, *, method=None):
            request = Request("http://127.0.0.1:8300" + path,
                              data=json.dumps(body).encode() if body is not None else None, method=method,
                              headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            try:
                with urlopen(request) as result:
                    return result.status, None if result.status == 204 else json.load(result)
            except HTTPError as error:
                return error.code, json.load(error)
        def submit(token):
            return call(token, "/v1/jobs", {"url": "https://www.instagram.com/reel/ExampleReel/", "request_id": str(uuid4())})
        free, paid, admin = [account(p) for p in ("free", "paid", "admin")]
        with ThreadPoolExecutor(max_workers=10) as pool:
            free_results = list(pool.map(submit, [free] * 14))
            paid_results = list(pool.map(submit, [paid] * 53))
        self.assertEqual(sum(s == 200 for s, _ in free_results), 5)
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
        self.assertEqual(call(free, path, method="DELETE")[0], 404)
        self.assertEqual(call(paid, path, method="DELETE")[0], 409)
        import gridfs
        bucket = gridfs.GridFS(media_db())
        artifact = bucket.put(b"test video")
        db().jobs.update_one({"_id": job["_id"]}, {"$set": {
            "state": "done", "artifact": artifact}})
        ticket = call(paid, path + "/playback", {})[1]["ticket"]
        used = call(paid, "/v1/me")[1]["used"]
        for _ in range(2):
            self.assertEqual(call(paid, path, method="DELETE")[0], 204)
        self.assertTrue(bucket.exists(artifact))
        for suffix in ("", "/video", "/poster"):
            self.assertEqual(call(paid, path + suffix)[0], 404)
        self.assertEqual(call(paid, path + "/report", {"category":"Timing","note":"Test"})[0], 404)
        self.assertEqual(call(paid, "/v1/play/" + job["_id"] + "/" + ticket)[0], 404)
        self.assertNotIn(job["_id"], [item["id"] for item in call(paid, "/v1/jobs")[1]])
        self.assertEqual(call(paid, "/v1/me")[1]["used"], used)
        self.assertEqual(call(paid, "/v1/jobs", {"request_id": job["request_id"], "url": job["url"]})[0], 410)
        self.assertNotIn("report", db().jobs.find_one({"_id": job["_id"]}))
        restored = call(paid, "/v1/jobs", {"request_id": str(uuid4()), "url": job["url"]})
        self.assertEqual(restored[1]["id"], job["_id"])
        self.assertEqual(call(paid, "/v1/me")[1]["used"], used)
        self.assertEqual(call(paid, "/v1/play/" + job["_id"] + "/" + ticket)[0], 404)

        self.assertEqual(call("anonymous", "/v1/jobs")[0], 401)
        body = {"url": "https://www.instagram.com/reel/ExampleReel/?tracking=123", "request_id": str(uuid4())}
        self.assertEqual(call(admin, "/v1/jobs", body)[1]["id"], call(admin, "/v1/jobs", body)[1]["id"])
        self.assertIsNone(call(admin, "/v1/me")[1]["limit"])
        self.assertEqual(call(admin, "/v1/jobs", {**body,"url":"file:///private/file"})[0], 422)

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

    def test_sliding_media_cache(self):
        import gridfs
        from .api import resume_deletions

        bucket = gridfs.GridFS(media_db())
        user = "apple:" + str(uuid4())
        token = secrets.token_urlsafe(36)
        other = secrets.token_urlsafe(36)
        for key, subject in [(token, user), (other, "apple:" + str(uuid4()))]:
            db().sessions.insert_one({"_id": hash_value(key), "owner": subject, "expires": now() + timedelta(minutes=3)})

        def call(path, body=None, *, method=None, key=token):
            req = Request("http://127.0.0.1:8300/v1/" + path,
                          data=None if body is None else json.dumps(body).encode(), method=method,
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            try:
                with urlopen(req) as result:
                    if result.status == 204:
                        return result.status, None
                    if result.headers.get_content_type() == "application/json":
                        return result.status, json.load(result)
                    return result.status, result.read()
            except HTTPError as error:
                return error.code, None

        url = "https://www.instagram.com/reel/CacheTest/"
        def submission():
            return {"url": url + "?share=1", "request_id": str(uuid4())}
        original = submission()
        job = call("jobs", original)[1]["id"]
        selector = {"_id": job}
        path = "jobs/" + job
        video = b"disposable movie" * 30_000
        artifact = bucket.put(video, poster=b"image")
        db().jobs.update_one(selector, {"$set": {"state": "done", "artifact": artifact, "has_poster": True}})
        self.assertEqual(call(path + "/video")[1], video)
        self.assertEqual(call(path + "/received", {"bytes": 1})[0], 409)
        self.assertEqual(call(path + "/received", {"bytes": len(video)}, key=other)[0], 404)
        resume_deletions()
        self.assertAlmostEqual((db().jobs.find_one(selector)["media_expires"] - now()).total_seconds(), 7*86400, delta=5)
        self.assertEqual(call(path + "/received", {"bytes": len(video)})[0], 204)
        deadline = db().jobs.find_one(selector)["media_expires"]
        self.assertAlmostEqual((deadline - now()).total_seconds(), 86400, delta=5)
        self.assertEqual(call(path + "/received", {"bytes": len(video)})[0], 204)
        self.assertEqual(call("jobs")[0], 200)
        self.assertEqual(call(path)[0], 200)
        self.assertEqual(db().jobs.find_one(selector)["media_expires"], deadline)
        cached = submission()
        near_expiry = now() + timedelta(seconds=60)
        db().jobs.update_one(selector, {"$set": {"media_expires": near_expiry}})
        self.assertEqual(call("jobs", cached)[1]["id"], job)
        self.assertGreater(db().jobs.find_one(selector)["media_expires"], near_expiry + timedelta(hours=23))
        self.assertEqual(call("me")[1]["used"], 1)
        self.assertNotEqual(call("jobs", submission(), key=other)[1]["id"], job)
        conflicting = {**cached, "url": "https://www.instagram.com/reel/OtherTest/"}
        self.assertEqual(call("jobs", conflicting)[0], 409)
        ticket = call(path + "/playback", {})[1]["ticket"]
        db().jobs.update_one(selector, {"$set": {"media_expires": now() - timedelta(seconds=1)}})
        self.assertFalse(call(path)[1]["available_on_server"])
        self.assertEqual(call(path + "/video")[0], 410)
        self.assertEqual(call("play/" + job + "/" + ticket)[0], 410)
        self.assertEqual(call("jobs", cached)[1]["id"], job)
        resume_deletions()
        resume_deletions()
        self.assertFalse(bucket.exists(artifact))
        self.assertEqual(media_db()["fs.chunks"].count_documents({"files_id": artifact}), 0)
        self.assertEqual(call(path + "/poster")[0], 404)
        self.assertEqual(call("play/" + job + "/" + ticket)[0], 404)
        self.assertEqual(call("library/deletions", {"ids": [job]})[1], [])
        self.assertEqual([item["id"] for item in call("library/progress", {"ids": [job]})[1]], [job])
        self.assertEqual(call("library/progress", {"ids": [job]}, key=other)[1], [])
        self.assertNotEqual(call("jobs", submission())[1]["id"], job)
        self.assertEqual(call(path, method="DELETE")[0], 204)
        self.assertEqual(call("jobs", cached)[0], 410)
        self.assertEqual(call("library/deletions", {"ids": [job]})[1], [job])
        self.assertEqual(call("library/progress", {"ids": [job]})[1], [])
        self.assertEqual(call("library/deletions", {"ids": [job]}, key=other)[1], [])

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
        artifact = gridfs.GridFS(media_db()).put(data, filename=job)
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
