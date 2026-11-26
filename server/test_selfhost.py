"""Self-host authentication, admission and the matching-only execution contract."""
from concurrent.futures import ThreadPoolExecutor
import os
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException

from . import api, worker
from .db import db, now, setup_indexes
from .download import resolve_share
from .upload_voice import KITTEN_MATCH, KITTEN_MATCH_MODEL, correct_kitten_model


class LocalContract(TestCase):
    def test_only_the_private_key_authenticates(self):
        key = "a" * 64
        with patch.dict(os.environ, {"SELF_HOST_KEY": key}):
            self.assertEqual(api.owner("Bearer " + key), "self-host")
            for credential in ("", "Bearer " + "x" * 64, "Basic " + key):
                with self.assertRaises(HTTPException) as denied:
                    api.owner(credential)
                self.assertEqual(denied.exception.status_code, 401)
        with patch.dict(os.environ, {"SELF_HOST_KEY": ""}), self.assertRaises(HTTPException):
            api.owner("Bearer " + key)

    def test_share_link_only_follows_validated_instagram_reels(self):
        value = api.canonical_url("https://instagram.com/share/reel/token_12/?igsh=tracker")
        self.assertEqual(value, "https://www.instagram.com/share/reel/token_12/")
        with patch("server.download.HTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status, response.getheader.return_value = 302, "/reel/OriginalID/?igsh=z"
            self.assertEqual(resolve_share(value, "http://127.0.0.1:1234"),
                             "https://www.instagram.com/reel/OriginalID/")
            response.getheader.return_value = "https://example.org/reel/ExternalID/"
            with self.assertRaises(ValueError):
                resolve_share(value, "http://127.0.0.1:1234")

    def test_fallback_records_a_limited_user_facing_reason(self):
        self.assertIn("clear voice sample", worker.best_retry_reason("Translating", worker.VOICE_ERRORS["unclear_speaker"]))
        self.assertIn("translate", worker.best_retry_reason("Translating", ""))
        self.assertNotIn("private input", worker.best_retry_reason("Voicing", "private input"))

    def test_cache_and_result_verification_require_the_intended_model(self):
        job = {"voice_mode": "match", "quality_requested": "fast", "render_quality": "fast",
               "target_language": "en", "match_engine": KITTEN_MATCH}
        self.assertIn(KITTEN_MATCH, worker.quality_options(job))
        self.assertTrue(correct_kitten_model(job, KITTEN_MATCH_MODEL))
        self.assertFalse(correct_kitten_model(job, "Qwen3-TTS-12Hz-1.7B-Base"))
        job.pop("match_engine")
        job["render_quality"] = "best"
        self.assertEqual(worker.quality_options(job), ["--quality", "best"])
        self.assertFalse(correct_kitten_model(job, None))
        self.assertFalse(correct_kitten_model(job, "Kokoro v1.0"))


@skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1", "Requires disposable database")
class SelfHostQueue(TestCase):
    @classmethod
    def setUpClass(cls):
        setup_indexes()

    def setUp(self):
        self.user = "test:" + str(uuid4())
        self.url = "https://instagram.com/reel/MatchFixture/?x=1"
        self.enterContext(patch.dict(os.environ, {"REELDUB_KITTEN_MATCH": "1"}))
        self.addCleanup(db().jobs.delete_many, {"owner": self.user})

    def submit(self, **values):
        return api.submit(api.Submission(**({"url": self.url} | values)), self.user)

    def test_target_fallback_and_idempotent_replays(self):
        request = uuid4()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.submit(request_id=request, quality="fast", target_language="es"), range(4)))
        self.assertEqual(len({item["id"] for item in results}), 1)
        self.assertEqual((results[0]["quality_requested"], results[0]["quality"], results[0]["voice_mode"]),
                         ("fast", "best", "match"))
        self.assertTrue(results[0]["quality_fallback"])
        with self.assertRaises(HTTPException) as error:
            self.submit(request_id=request, quality="best", target_language="es")
        self.assertEqual(error.exception.status_code, 409)
        with patch.dict(os.environ, {"REELDUB_KITTEN_MATCH": "0"}):
            self.assertEqual(self.submit(quality="fast")["quality"], "best")

    def test_failed_kitten_requeues_once_and_cannot_be_cached_as_kitten(self):
        job = self.submit(quality="fast")
        self.assertEqual(job["quality"], "fast")
        fence = {"_id": job["id"], "lease": "first", "state": "running"}
        db().jobs.update_one({"_id": job["id"]}, {"$set": {"lease": "first", "state": "running"}})
        self.assertTrue(worker.retry_as_best(db().jobs, fence, "Using Best."))
        self.assertFalse(worker.retry_as_best(db().jobs, fence, "Using Best."))
        stored = db().jobs.find_one({"_id": job["id"]})
        self.assertEqual((stored["quality_requested"], stored["render_quality"], stored["state"]), ("fast", "best", "queued"))
        self.assertNotIn("match_engine", stored)
        db().jobs.update_one({"_id": job["id"]}, {"$set": {"state": "done", "artifact": "test-only"}})
        self.assertNotEqual(self.submit(quality="fast")["id"], job["id"])
        with patch.dict(os.environ, {"REELDUB_KITTEN_MATCH": "0"}):
            self.assertEqual(self.submit(quality="fast")["id"], job["id"])

    def test_worker_disappearing_after_admission_uses_qwen(self):
        job = self.submit(quality="fast")
        work = worker.claim(studio=True, kitten_match=False)
        self.assertEqual((work["_id"], work["quality_requested"], work["render_quality"]), (job["id"], "fast", "best"))
        self.assertNotIn("match_engine", work)

    def test_upload_source_is_private_and_needs_no_purchase(self):
        from bson import ObjectId
        from datetime import timedelta
        source = ObjectId()
        db().sources.insert_one({"_id":source,"identity":"upload:example","owner":self.user,
            "ready":True,"expires":now()+timedelta(hours=1),"seconds":8,"bytes":10,"title":"My clip"})
        self.addCleanup(db().sources.delete_one, {"_id":source})
        body = api.Submission(source_id=str(source), quality="best")
        self.assertEqual(api.submit(body, self.user)["source_id"], str(source))
        with self.assertRaises(HTTPException) as denied:
            api.submit(body, "other-user")
        self.assertEqual(denied.exception.status_code, 404)
