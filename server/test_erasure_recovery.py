"""Restore and account-deletion regression tests; only use a disposable database."""
from datetime import timedelta
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import gridfs
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import HTTPException

from . import api, erasure, erasure_journal, erasure_replay
from .db import db, media_db, now


class JournalTests(unittest.TestCase):
    def test_signin_client_secret_has_a_short_lifetime_and_matches_app(self):
        private_key = ec.generate_private_key(ec.SECP256R1())
        with TemporaryDirectory() as folder:
            path = Path(folder) / "test.p8"
            path.write_bytes(private_key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            with patch.dict(os.environ, {"APPLE_SIGNIN_TEAM_ID": "fixture-team",
                        "APPLE_SIGNIN_KEY_ID": "fixture-key", "APPLE_SIGNIN_PRIVATE_KEY_FILE": str(path)}):
                signed = erasure.apple_client_secret("test.reeldub")
        claims = jwt.decode(signed, private_key.public_key(), algorithms=["ES256"],
                            audience="https://appleid.apple.com", issuer="fixture-team")
        self.assertEqual(jwt.get_unverified_header(signed)["kid"], "fixture-key")
        self.assertEqual(claims["sub"], "test.reeldub")
        self.assertEqual(claims["exp"] - claims["iat"], 300)

    def test_only_revokes_tokens_exchanged_for_the_verified_apple_subject(self):
        signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode({"iat": now(), "exp": now() + timedelta(minutes=5),
                            "sub": "matching", "aud": "app", "iss": "https://appleid.apple.com"},
                           signing_key, algorithm="RS256")
        keys = Mock()
        keys.get_signing_key_from_jwt.return_value.key = signing_key.public_key()
        with patch.dict(os.environ, {"APPLE_CLIENT_ID": "app"}), patch.object(
                erasure, "apple_client_secret", return_value="test"), patch.object(erasure.requests, "post") as post:
            exchanged = Mock()
            exchanged.json.return_value = {"id_token": token, "refresh_token": "dummy"}
            post.side_effect = [exchanged, exchanged, Mock(status_code=200)]
            self.assertFalse(erasure.revoke_apple("code", "apple:other", keys))
            self.assertEqual(post.call_count, 1)
            self.assertTrue(erasure.revoke_apple("code", "apple:matching", keys))
            url, = post.call_args.args
            self.assertEqual(url, "https://appleid.apple.com/auth/revoke")
            self.assertEqual(post.call_args.kwargs["data"]["token_type_hint"], "refresh_token")

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

    def test_confirmation_requires_configured_mirror(self):
        with TemporaryDirectory() as journal, patch.dict(os.environ, {
            "REELDUB_ERASURE_JOURNAL_DIR": journal, "REELDUB_ERASURE_MIRROR_COMMAND": "/usr/bin/false"
        }), patch.object(api, "verify_identity", return_value={"sub": "alice"}), patch.object(api, "db") as database:
            with self.assertRaises(HTTPException) as result:
                api.delete_account(api.DeleteAccount(identity_token="fixture", challenge="a" * 32), "apple:alice")
            self.assertEqual(result.exception.status_code, 503)
            database.assert_not_called()
            self.assertFalse(list(Path(journal).glob("*.json")))


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

    def test_live_erasure_retries_pending_cleanup(self):
        owner = "apple:" + uuid4().hex
        database = db()
        database.accounts.insert_one({"_id": owner, "video_suggestion": {"note": "personal"}})
        database.sessions.insert_one({"_id": uuid4().hex, "owner": owner})
        source = gridfs.GridFS(media_db()).put(b"private input")
        database.sources.insert_one({"_id": source, "owner": owner})
        body = api.DeleteAccount(identity_token="fixture", challenge="a" * 32)
        with TemporaryDirectory() as journal, patch.dict(os.environ, {
            "REELDUB_ERASURE_JOURNAL_DIR": journal, "REELDUB_ERASURE_MIRROR_COMMAND": ""
        }), patch.object(api, "verify_identity", return_value={"sub": owner[6:]}):
            with patch.object(erasure, "erase", side_effect=OSError("temporary file issue")):
                self.assertEqual(api.delete_account(body, owner)["state"], "pending")
            self.assertTrue(erasure.closing(owner))
            erasure.resume()
            self.assertIsNone(database.accounts.find_one({"_id": owner}))
            self.assertIsNone(database.sessions.find_one({"owner": owner}))
            self.assertFalse(gridfs.GridFS(media_db()).exists(source))
            self.assertIn("expires", database.account_deletions.find_one({"_id": owner}))
            self.assertEqual(len(list(Path(journal).glob("*.json"))), 1)
