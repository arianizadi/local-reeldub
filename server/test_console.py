"""Authorization and state transitions against the disposable hosted metadata stores."""

import os
from datetime import timedelta
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

import socket
import threading
import time
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
import requests
import uvicorn
from . import api, console, erasure
from .db import db, now, separate_metadata, setup_indexes, uses_postgres
from .postgres import install


@skipUnless(
    os.getenv("REELDUB_TEST_DATABASE") == "1" and separate_metadata(),
    "Isolated metadata stores only",
)
class ConsoleTests(TestCase):
    @classmethod
    def setUpClass(cls):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        base = "http://127.0.0.1:" + str(sock.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(api.app, lifespan="off", access_log=False, log_level="error")
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()

        def close():
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()

        cls.addClassCleanup(close)
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("Fixture server failed to start")
        cls.client = SimpleNamespace(
            get=lambda path, **kw: requests.get(base + path, timeout=5, **kw),
            post=lambda path, **kw: requests.post(base + path, timeout=5, **kw),
            put=lambda path, **kw: requests.put(base + path, timeout=5, **kw),
        )

    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "ADMIN_APPLE_SUB": "console-fixture",
                "REELDUB_ACCESS_MODE": "hosted",
                "REELDUB_OUTPUT_PROFILE": "studio",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        if uses_postgres():
            with db().connection() as conn:
                install(conn)
        else:
            setup_indexes()
        db().request_rates.delete_many(
            {
                "_id": api.hash_value("apple:console-fixture")
                + "/console-unlock/"
                + str(int(time.time()) // 60)
            }
        )
        self.admin = self.session("apple:console-fixture")
        challenge = self.client.post("/v1/apple/challenge").json()["challenge"]
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        stamp = now()
        proof = {
            "identity_token": jwt.encode(
                {
                    "iss": "https://appleid.apple.com",
                    "sub": "console-fixture",
                    "aud": os.environ["APPLE_CLIENT_ID"],
                    "exp": stamp + timedelta(minutes=2),
                    "iat": stamp,
                    "nonce": api.hash_value(challenge),
                },
                private,
                algorithm="RS256",
            ),
            "challenge": challenge,
        }
        with patch.object(
            api.apple_keys,
            "get_signing_key_from_jwt",
            return_value=SimpleNamespace(key=private.public_key()),
        ):
            reply = self.client.post("/v1/admin/unlock", json=proof, headers=self.admin)
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(
                self.client.post(
                    "/v1/admin/unlock", json=proof, headers=self.admin
                ).status_code,
                401,
            )
        self.admin["X-ReelDub-Admin"] = reply.json()["token"]
        self.addCleanup(
            db().jobs.delete_many,
            {
                "owner": "apple:console-fixture",
                "url": "https://www.instagram.com/reel/ABCDE/",
            },
        )
        self.subject = "apple:" + uuid4().hex
        self.member = self.session(self.subject)
        self.reviewer = self.session(api.BETA_REVIEWER, purpose="beta-review")
        db().accounts.update_one(
            {"_id": self.subject},
            {"$set": {"tier": "paid", "email": "fixture@example.invalid"}},
            upsert=True,
        )

    def session(self, owner, **fields):
        secret = uuid4().hex
        db().sessions.insert_one(
            {
                "_id": api.hash_value(secret),
                "owner": owner,
                "expires": now() + timedelta(minutes=5),
                **fields,
            }
        )
        return {"Authorization": "Bearer " + secret}

    def test_video_suggestion_is_optional_private_editable_and_erased_with_account(self):
        path = "/v1/me/video-suggestion"
        first = {"topic": "longer", "note": "  Concerts  "}
        revised = {"topic": "other_apps", "note": "Short documentaries"}
        self.assertEqual(self.client.put(path, json=first).status_code, 401)
        self.assertEqual(self.client.put(path, json=first, headers=self.reviewer).status_code, 403)
        self.assertIsNone(self.client.get(path, headers=self.member).json()["suggestion"])
        self.assertEqual(self.client.put(path, json={"topic":"other", "note":"  "}, headers=self.member).status_code, 422)
        self.assertEqual(self.client.put(path, json={**first, "note":"a"*401}, headers=self.member).status_code, 422)
        self.assertEqual(self.client.put(path, json={**first, "owner":"someone-else"}, headers=self.member).status_code, 422)
        self.assertEqual(self.client.put(path, json=first, headers=self.member).status_code, 204)
        self.assertEqual(self.client.get(path, headers=self.member).json()["suggestion"]["note"], "Concerts")
        self.assertEqual(self.client.put(path, json=revised, headers=self.member).status_code, 204)
        self.assertIsNone(self.client.get(path, headers=self.admin).json()["suggestion"])
        rows = self.client.post("/v1/admin/accounts", json={}, headers=self.admin).json()["rows"]
        self.assertEqual(next(row for row in rows if row["id"] == self.subject)["video_suggestion"], revised)
        self.assertEqual(self.client.post("/v1/admin/accounts", json={}, headers=self.member).status_code, 403)
        db().accounts.update_one({"_id":self.subject}, {"$set":{"banned":True}})
        self.assertEqual(self.client.put(path, json=first, headers=self.member).status_code, 403)
        erasure.erase(self.subject)
        self.assertNotIn("video_suggestion", db().accounts.find_one({"_id":self.subject}))

    def test_console_bound_to_pinned_hosted_apple_session(self):
        for who, status in [
            ({}, 401),
            (self.member, 403),
            (self.reviewer, 403),
            (self.admin, 200),
        ]:
            self.assertEqual(
                self.client.get("/v1/admin/overview", headers=who).status_code, status
            )
        self.assertEqual(
            self.client.get(
                "/v1/admin/overview",
                headers={"Authorization": self.admin["Authorization"]},
            ).status_code,
            403,
        )
        other_session = self.session("apple:console-fixture")
        other_session["X-ReelDub-Admin"] = self.admin["X-ReelDub-Admin"]
        self.assertEqual(
            self.client.get("/v1/admin/overview", headers=other_session).status_code,
            403,
        )
        db().accounts.update_one({"_id": self.subject}, {"$set": {"tier": "admin"}})
        for route in ["activity", "accounts", "moderation", "experiments"]:
            self.assertEqual(
                self.client.post(
                    "/v1/admin/" + route, headers=self.member, json={}
                ).status_code,
                403,
            )
        self.assertFalse(console.permitted(self.subject))
        funding = self.client.post(
            "/v1/admin/allowance", headers=self.admin, json={"account": self.subject}
        )
        self.assertEqual(funding.status_code, 200, funding.text)
        from .db import media_db

        media_db().signin_challenges.update_one(
            {"_id": "admin/" + api.hash_value(self.admin["X-ReelDub-Admin"])},
            {"$set": {"expires": now() - timedelta(seconds=1)}},
        )
        self.assertEqual(
            self.client.get("/v1/admin/overview", headers=self.admin).status_code, 403
        )
        with patch.dict(os.environ, {"REELDUB_ACCESS_MODE": "operator"}):
            self.assertFalse(console.permitted("self-host"))
        with patch.dict(os.environ, {"ADMIN_APPLE_SUB": ""}):
            self.assertFalse(console.permitted("apple:console-fixture"))

    def test_ban_revokes_hosted_work_and_erasure_scrubs_tombstone(self):
        job = str(uuid4())
        db().jobs.insert_one(
            {
                "_id": job,
                "owner": self.subject,
                "request_id": job,
                "state": "queued",
                "created": now(),
                "slot": 0,
            }
        )
        change = {"account": self.subject, "ban": True, "reason": "Repeated abuse"}
        self.assertEqual(
            self.client.post(
                "/v1/admin/moderation", headers=self.admin, json=change
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/v1/jobs", headers=self.member).status_code, 403
        )
        self.assertEqual(
            self.client.get("/v1/me", headers=self.member).json()["tier"], "suspended"
        )
        self.assertFalse(
            api.admit_job(
                {"_id": str(uuid4()), "owner": self.subject, "request_id": str(uuid4())}
            )
        )
        row = db().jobs.find_one({"_id": job})
        self.assertEqual(row["state"], "failed")
        self.assertNotIn("slot", row)
        key = uuid4().hex
        db().playback.insert_one(
            {
                "_id": key,
                "job": job,
                "owner": self.subject,
                "expires": now() + timedelta(minutes=1),
            }
        )
        self.assertIsNone(api.playback_grant(key, job, now()))
        other_key = uuid4().hex
        db().playback.insert_one(
            {
                "_id": other_key,
                "job": job,
                "owner": "apple:console-fixture",
                "expires": now() + timedelta(minutes=1),
            }
        )
        self.assertEqual(
            api.playback_grant(other_key, job, now())["owner"], "apple:console-fixture"
        )
        erasure.erase(self.subject)
        self.assertEqual(
            set(db().accounts.find_one({"_id": self.subject})),
            {"_id", "banned", "moderated_at", "erased"},
        )
        self.assertEqual(
            self.client.post(
                "/v1/admin/moderation",
                headers=self.admin,
                json={**change, "account": "apple:console-fixture"},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/v1/admin/moderation",
                headers=self.admin,
                json={**change, "ban": False},
            ).status_code,
            200,
        )
        self.assertIsNone(db().accounts.find_one({"_id": self.subject}))

    def test_experiment_separate_from_cache_retries_and_stats(self):
        body = {
            "request_id": str(uuid4()),
            "mode": "fast",
            "target": "en",
            "url": "https://www.instagram.com/reel/ABCDE/",
        }
        send = lambda b: self.client.post(
            "/v1/admin/experiments", json=b, headers=self.admin
        )
        first = send(body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["id"], send(body).json()["id"])
        self.assertEqual(send({**body, "mode": "medium"}).status_code, 409)
        self.assertEqual(
            send({**body, "target": "de", "voice": "preset"}).status_code, 422
        )
        self.assertEqual(send({**body, "url": "https://example.org"}).status_code, 422)
        self.assertEqual(send({**body, "engine_path": "ignored"}).status_code, 422)
        job = first.json()["id"]
        db().jobs.update_one(
            {"_id": job},
            {
                "$set": {
                    "state": "done",
                    "artifact": "demo",
                    "started": now() - timedelta(seconds=15),
                    "finished": now(),
                    "media_seconds": 6,
                    "source_language": "ru",
                }
            },
        )
        retry_key = str(uuid4())
        for key in [retry_key, retry_key, str(uuid4())]:
            db().jobs.update_one(
                {"_id": job},
                [
                    {
                        "$set": {
                            "cache_hits": api.cache_hit_count(key),
                            "request_keys": {"$setUnion": ["$request_keys", [key]]},
                        }
                    }
                ],
            )
        self.assertEqual(db().jobs.find_one({"_id": job})["cache_hits"], 2)
        self.assertIsNone(
            api.minutes.existing("apple:console-fixture", body["url"], "en", False)
        )
        page = self.client.post(
            "/v1/admin/activity",
            json={"account": "apple:console-fixture"},
            headers=self.admin,
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn(job, [j["id"] for j in page.json()["rows"]])
        self.assertNotIn("request_keys", page.json()["rows"][0])
        self.assertGreaterEqual(console.metrics()["cache_hits"], 2)
