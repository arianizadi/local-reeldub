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
    def test_direct_origin_auth_and_server_side_plan(self):
        user = "apple:" + str(uuid4())
        token = secrets.token_urlsafe(32)
        body = {"url": "https://www.instagram.com/reel/AccessTest/", "request_id": str(uuid4())}
        def status(data, bearer=None):
            headers = {"Content-Type": "application/json"}
            if bearer:
                headers["Authorization"] = "Bearer " + bearer
            req = Request("http://127.0.0.1:8300/v1/jobs", data=json.dumps(data).encode(), headers=headers)
            try:
                with urlopen(req) as response:
                    return response.status
            except HTTPError as e:
                return e.code
        before = db().jobs.count_documents({})
        self.assertEqual(status(body), 401)
        self.assertEqual(status(body, secrets.token_urlsafe(48)), 401)
        db().sessions.insert_one({"_id": hash_value(token), "owner": user, "expires": now()-timedelta(seconds=1)})
        self.assertEqual(status(body, token), 401)
        db().sessions.update_one({"_id": hash_value(token)}, {"$set": {"expires": now()+timedelta(minutes=1)}})
        self.assertEqual(status({**body, "tier": "admin", "priority": 1}, token), 422)
        self.assertEqual(status({**body, "url": "https://example.org/a.mp4"}, token), 422)
        self.assertEqual(db().jobs.count_documents({}), before)
        with patch.dict(os.environ, {"REELDUB_ACCESS_MODE": "hosted", "SELF_HOST_KEY": "x"*48}):
            with self.assertRaises(HTTPException) as rejected:
                owner("Bearer " + "x"*48)
            self.assertEqual(rejected.exception.status_code, 401)
        with patch.dict(os.environ, {"REELDUB_ACCESS_MODE": "operator", "SELF_HOST_KEY": "x"*48}):
            self.assertEqual(owner("Bearer " + "x"*48), "self-host")
            with self.assertRaises(HTTPException):
                owner("Bearer " + token)

    def test_apple_review_key_scope_expiry_and_signout(self):
        from .api import BETA_REVIEWER, PurchaseIntent, logout, me, owned, prepare_purchase
        from . import store
        token = secrets.token_urlsafe(32)
        auth = "Bearer " + token
        key = {"_id": hash_value(token)}
        db().sessions.insert_one({**key, "owner": BETA_REVIEWER, "expires": now() + timedelta(days=90)})
        self.addCleanup(db().sessions.delete_one, key)
        with self.assertRaises(HTTPException):
            owner(auth)
        db().sessions.update_one(key, {"$set": {"purpose": "beta-review"}})
        self.assertEqual(owner(auth), BETA_REVIEWER)
        profile = me(owner(auth), "1")
        self.assertEqual((profile["tier"], profile["limit"]), ("free", 5))
        self.assertFalse(profile["purchase_preflight"])
        with self.assertRaises(HTTPException) as caught:
            prepare_purchase(PurchaseIntent(product=store.MINUTE_PACK), owner(auth))
        self.assertEqual(caught.exception.status_code, 403)
        other = {"_id": str(uuid4()), "owner": "apple:" + str(uuid4()), "request_id": str(uuid4())}
        db().jobs.insert_one(other)
        self.addCleanup(db().jobs.delete_one, {"_id": other["_id"]})
        with self.assertRaises(HTTPException) as caught:
            owned(other["_id"], owner(auth))
        self.assertEqual(caught.exception.status_code, 404)
        logout(auth, owner(auth))
        self.assertEqual(owner(auth), BETA_REVIEWER)
        db().sessions.update_one(key, {"$set": {"expires": now() - timedelta(seconds=1)}})
        with self.assertRaises(HTTPException):
            owner(auth)

    def test_beta_allowance_requires_verified_email_and_stays_in_standard_queue(self):
        from .api import apple, AppleLogin, beta_invite_id, me, submit, Submission

        email = f"{uuid4()}@example.org"
        subjects = [str(uuid4()) for _ in range(3)]
        db().beta_allowances.insert_one({"_id": beta_invite_id(email.upper()), "limit": 50})
        login = AppleLogin(identity_token="x" * 128, challenge="y" * 48)
        for subject, address, verified, limit in (
            (subjects[0], email, False, 5),
            (subjects[0], email + ".other", True, 5),
            (subjects[1], email, "true", 50),
            (subjects[2], email, True, 5),
        ):
            claims = {"sub": subject, "email": address, "email_verified": verified}
            with patch("server.api.verify_identity", return_value=claims):
                apple(login)
            self.assertEqual(me("apple:" + subject)["limit"], limit)

        user = "apple:" + subjects[1]
        day = now().date().isoformat()
        for i in range(49):
            key = str(uuid4())
            db().jobs.insert_one({"_id": key, "request_id": key, "owner": user,
                                  "day": day, "slot": i, "state": "done"})
        item = Submission(url="https://www.instagram.com/reel/BetaDaily/", request_id=uuid4())
        self.assertEqual(submit(item, user)["lane"], "free")
        self.assertEqual(submit(item, user)["lane"], "free")
        self.assertEqual(me(user)["used"], 50)
        with self.assertRaises(HTTPException) as caught:
            submit(item.model_copy(update={"request_id": uuid4()}), user)
        self.assertEqual(caught.exception.status_code, 429)
        with patch.dict(os.environ, {"REELDUB_CREATOR_MINUTES": "1"}):
            self.assertEqual(me(user)["limit"], 50)

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
        media_db()["fs.files"].update_many({"_id": {"$in": items}}, {"$set": {"uploadDate": now()-timedelta(hours=3)}})
        for fixture in [
            {"_id": running, "owner": user, "request_id": str(uuid4()), "state": "running", "lease": "lease"},
            {"_id": published, "owner": user, "request_id": str(uuid4()), "state": "done", "artifact": items[1]}]:
            db().jobs.insert_one(fixture)
        purge_orphaned_uploads()
        self.assertEqual([bucket.exists(i) for i in items], [True, True, False])
