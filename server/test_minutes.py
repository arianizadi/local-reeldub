"""Integration coverage on a disposable MongoDB with the Creator pilot enabled."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from bson import ObjectId
from fastapi import HTTPException

from . import api, minutes, store
from .db import db, now, setup_indexes


@skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1", "Requires disposable MongoDB")
class CreatorTests(TestCase):
    def setUp(self):
        setup_indexes()
        self.config = patch.dict(
            os.environ, {"REELDUB_CREATOR_MINUTES": "1", "REELDUB_OUTPUT_PROFILE": "studio"}
        )
        self.config.start()
        self.addCleanup(self.config.stop)
        self.owner = "apple:" + str(uuid4())
        db().accounts.insert_one({"_id": self.owner, "tier": "free"})

    def source(self, seconds):
        ident = ObjectId()
        db().sources.insert_one(
            {
                "_id": ident,
                "owner": self.owner,
                "identity": "upload:" + str(ident),
                "ready": True,
                "title": "Local fixture",
                "expires": now() + timedelta(hours=1),
                "seconds": seconds,
            }
        )
        return str(ident)

    def quote(self, source, targets=("en",), trim=False):
        return api.create_quote(api.QuoteRequest(source_id=source, targets=targets, trim=trim), self.owner)

    def pay(self, product):
        monthly = product == store.CREATOR
        tx = SimpleNamespace(
            productId=product,
            rawEnvironment="Production",
            transactionId=str(uuid4()),
            signedDate=100,
            quantity=1,
            revocationDate=None,
            isUpgraded=False,
            expiresDate=None,
            rawInAppOwnershipType="PURCHASED",
            appAccountToken=store.store_token(self.owner),
            rawType="Auto-Renewable Subscription" if monthly else "Consumable",
        )
        if monthly:
            tx.expiresDate = int((now() + timedelta(days=30)).timestamp() * 1000)
        store.record(tx, self.owner)
        return tx

    def confirm(self, q):
        return minutes.confirm(self.owner, q["id"])

    def test_kitten_narrator_is_a_private_english_choice_with_independent_cache(self):
        source = self.source(10)
        self.enterContext(patch.dict(os.environ, {"REELDUB_ACCESS_MODE": "hosted", "ADMIN_APPLE_SUB": self.owner[6:],
                                                  "REELDUB_KITTEN_NARRATOR": "1", "REELDUB_CREATOR_MINUTES": "0"}))

        def request(mode="narrator", targets=("en",), engine="kitten2"):
            return api.create_quote(api.QuoteRequest(source_id=source, targets=targets, voice_mode=mode,
                                                     narrator_engine=engine), self.owner)

        self.assertEqual(api.me(self.owner)["kitten_narrator_languages"], ["en"])
        self.assertNotIn("kitten_narrator_languages", api.me("apple:another"))
        self.assertFalse(minutes.kitten_permitted("review"))
        for mode, target in [("match", "en"), ("narrator", "fr")]:
            with self.assertRaises(HTTPException):
                request(mode=mode, targets=[target])
        quote = request()
        with patch.dict(os.environ, {"ADMIN_APPLE_SUB": "other"}):
            with self.assertRaises(HTTPException) as revoked:
                self.confirm(quote)
            self.assertEqual(revoked.exception.status_code, 403)
            with self.assertRaises(HTTPException):
                request()
        kitten, = self.confirm(quote)
        original, = self.confirm(request(engine=None))
        self.assertEqual(kitten["narrator_engine"], "kitten2")
        self.assertIsNone(original.get("narrator_engine"))
        self.assertNotEqual(original["id"], kitten["id"])
        self.assertEqual(self.confirm(request())[0]["id"], kitten["id"])
        self.assertEqual(self.confirm(request(engine=None))[0]["id"], original["id"])

    def test_uploaded_daily_allowance_without_storekit_and_private_voice_cache(self):
        with patch.dict(os.environ, {"REELDUB_CREATOR_MINUTES": "0", "REELDUB_REEL_LINKS": "0"}):
            src = self.source(120)
            caps = api.me(self.owner)
            self.assertTrue(caps["video_uploads"])
            self.assertFalse(caps["reel_links"])
            self.assertNotIn("de", caps["upload_voice_modes"]["narrator"])
            self.assertIn("de", caps["upload_voice_modes"]["match"])
            with patch.object(minutes, "funding", side_effect=AssertionError("StoreKit should stay idle")):
                quote = self.quote(src)
                narrated, = self.confirm(quote)
                self.assertEqual(narrated["voice_mode"], "narrator")
                self.assertEqual(self.confirm(self.quote(src))[0]["id"], narrated["id"])
                with self.assertRaises(HTTPException):
                    minutes.confirm("apple:other", quote["id"])
                own = minutes.quote(self.owner, src, ["en"], False, "match", "own_voice")
                matched, = self.confirm(own)
                guest = minutes.quote(self.owner, src, ["en"], False, "match", "documented_permission", " release-1 ")
                approved, = self.confirm(guest)
                self.assertEqual(len({narrated["id"], matched["id"], approved["id"]}), 3)
                job = db().jobs.find_one({"_id": approved["id"]})
                self.assertEqual((job["voice_consent_version"], job["consent_reference"]), (1, "release-1"))
                self.assertIn("voice_consented_at", job)
                self.assertNotIn("consent_reference", approved)
                for _ in range(2):
                    self.confirm(self.quote(self.source(100)))
                short = self.quote(self.source(120))
                self.assertEqual(short["lines"][0]["missing"], 120)
                with self.assertRaises(HTTPException) as fail:
                    self.confirm(short)
                self.assertEqual(fail.exception.status_code, 402)

    def test_upload_matching_requires_appropriate_consent_and_supported_language(self):
        src = self.source(25)
        for mode, scope, reference, target in (
            ("match", None, None, "en"),
            ("match", "documented_permission", "   ", "en"),
            ("match", "own_voice", None, "hi"),
            ("match", "own_voice", "unneeded", "en"),
            ("narrator", "own_voice", None, "en"),
            ("narrator", None, None, "de"),
        ):
            with self.subTest(mode=mode, consent=scope, reference=reference, target=target):
                with self.assertRaises(HTTPException) as fail:
                    minutes.quote(self.owner, src, [target], False, mode, scope, reference)
                self.assertEqual(fail.exception.status_code, 422)
        with patch.dict(os.environ, {"REELDUB_OUTPUT_PROFILE": "light"}):
            with self.assertRaises(HTTPException):
                minutes.quote(self.owner, src, ["en"], False, "match", "own_voice")

    def test_upload_only_server_rejects_staged_and_direct_reels(self):
        src = self.source(23)
        url = "https://www.instagram.com/reel/ArchivedFixture/"
        db().sources.update_one({"_id": ObjectId(src)}, {"$set": {"identity": url}})
        quote = self.quote(src)
        with patch.dict(os.environ, {"REELDUB_REEL_LINKS": "0"}):
            for attempt in (lambda: self.confirm(quote), lambda: self.quote(src),
                            lambda: api.submit(api.Submission(url=url, request_id=uuid4()), self.owner),
                            lambda: api.stage_reel(api.ReelSource(url=url), self.owner)):
                with self.assertRaises(HTTPException) as rejected:
                    attempt()
                self.assertEqual(rejected.exception.status_code, 403)
        self.assertEqual(db().jobs.count_documents({"owner": self.owner}), 0)

    def test_studio_best_reuse_keeps_legacy_jobs_and_funding_separate(self):
        source = self.source(25)
        with patch.dict(os.environ, {"REELDUB_OUTPUT_PROFILE": "light"}):
            original_quote = self.quote(source)
            original, = self.confirm(original_quote)
            self.assertIsNone(original["render_quality"])
            db().jobs.update_one({"_id": original["id"]}, {"$set": {"state": "done", "artifact": ObjectId()}})
            old_reuse_quote = self.quote(source)
            self.assertTrue(old_reuse_quote["lines"][0]["reused"])

        with self.assertRaises(HTTPException) as changed:
            self.confirm(old_reuse_quote)
        self.assertEqual(changed.exception.status_code, 409)
        self.assertEqual(self.confirm(original_quote)[0]["id"], original["id"])

        fresh = self.quote(source)
        self.assertFalse(fresh["lines"][0]["reused"])
        best, = self.confirm(fresh)
        self.assertEqual(best["render_quality"], "best")
        self.assertIsNone(best["lab_mode"])
        self.assertNotEqual(best["id"], original["id"])
        db().jobs.update_one({"_id": best["id"]}, {"$set": {"state": "done", "artifact": ObjectId()}})
        again = self.quote(source)
        self.assertTrue(again["lines"][0]["reused"])
        self.assertEqual(self.confirm(again)[0]["id"], best["id"])
        self.assertEqual(db().jobs.count_documents({"owner":self.owner, "slot":{"$exists":True}}), 2)

    def test_direct_best_submit_replays_the_same_request_and_separates_old_cache(self):
        def submission():
            return api.Submission(url="https://www.instagram.com/reel/DEFAULT/", request_id=uuid4())
        old_request = submission()
        with patch.dict(os.environ, {"REELDUB_CREATOR_MINUTES":"0"}):
            with patch.dict(os.environ, {"REELDUB_OUTPUT_PROFILE":"light"}):
                original = api.submit(old_request, self.owner)
                db().jobs.update_one({"_id":original["id"]}, {"$set":{"state":"done", "artifact":ObjectId()}})
            self.assertEqual(api.submit(old_request, self.owner)["id"], original["id"])
            best = api.submit(submission(), self.owner)
            self.assertEqual(best["render_quality"], "best")
            self.assertIsNone(best["lab_mode"])
            self.assertNotEqual(best["id"], original["id"])
            db().jobs.update_one({"_id":best["id"]}, {"$set":{"state":"done", "artifact":ObjectId(), "source_seconds":24}})
            self.assertEqual(api.submit(submission(), self.owner)["id"], best["id"])

    def test_source_limit_check_covers_trim_legacy_quotes_and_cached_direct_requests(self):
        from .source import TOO_LONG
        from . import sources
        source = self.source(120)
        self.pay(store.MINUTE_PACK)
        quote = self.quote(source, trim=True)
        db().sources.update_one({"_id": ObjectId(source)}, {"$set": {"seconds": 121}})
        for attempt in [lambda: self.confirm(quote), lambda: self.quote(source, trim=True)]:
            with self.assertRaises(HTTPException) as rejected:
                attempt()
            self.assertEqual((rejected.exception.status_code, rejected.exception.detail), (422, TOO_LONG))
        self.assertEqual(db().jobs.count_documents({"owner": self.owner}), 0)
        self.assertEqual(api.me(self.owner)["pack_seconds"], 900)
        with patch.dict(os.environ, {"REELDUB_CREATOR_MINUTES": "0"}):
            def request():
                return api.Submission(url="https://www.instagram.com/reel/OldDub/", request_id=uuid4())
            original = request()
            job = api.submit(original, self.owner)
            key = {"_id": job["id"]}
            db().jobs.update_one(key, {"$set": {"state": "done", "artifact": ObjectId(), "media_seconds": 60}})
            with patch.object(sources, "prepare_reel", return_value={"seconds": 121}), self.assertRaises(HTTPException) as rejected:
                api.submit(request(), self.owner)
            self.assertEqual(rejected.exception.detail, TOO_LONG)
            self.assertEqual(api.submit(original, self.owner)["id"], job["id"])
            with patch.object(sources, "prepare_reel", return_value={"seconds": 120}) as prepare:
                self.assertEqual(api.submit(request(), self.owner)["id"], job["id"])
                self.assertEqual(api.submit(request(), self.owner)["id"], job["id"])
            prepare.assert_called_once()

    def test_beta_quote_and_confirm_use_the_same_daily_cap(self):
        db().beta_allowances.insert_one({"_id": str(uuid4()), "owner": self.owner, "limit": 50})
        for slot in range(49):
            key = str(uuid4())
            db().jobs.insert_one({"_id": key, "request_id": key, "owner": self.owner,
                                  "day": now().date().isoformat(), "slot": slot, "state": "done"})
        first = self.quote(self.source(24))
        competing = self.quote(self.source(23))
        self.assertEqual(first["free_left"], 0)
        self.assertEqual(self.confirm(first)[0]["lane"], "free")
        with self.assertRaises(HTTPException) as changed:
            self.confirm(competing)
        self.assertEqual(changed.exception.status_code, 409)
        self.assertEqual(self.quote(self.source(30))["lines"][0]["missing"], 30)

    def test_free_trim_ownership_and_legacy_route(self):
        long = self.source(61)
        self.assertEqual(self.quote(long)["lines"][0]["missing"], 61)
        q = self.quote(long, ("en", "es"), True)
        self.assertEqual(q["free_left"], 3)
        self.assertTrue(all(x["free"] == 1 for x in q["lines"]))
        with self.assertRaises(HTTPException):
            minutes.confirm("apple:other", q["id"])
        with self.assertRaises(HTTPException):
            minutes.quote("apple:other", long, ["en"], False)
        jobs = self.confirm(q)
        self.assertEqual([j["id"] for j in self.confirm(q)], [j["id"] for j in jobs])
        self.assertEqual(db().jobs.find_one({"_id": jobs[0]["id"]})["quoted_seconds"], 60)
        with self.assertRaises(HTTPException) as fail:
            api.submit(
                api.Submission(url="https://instagram.com/reel/FakeTest/", request_id=uuid4()), self.owner
            )
        self.assertEqual(fail.exception.status_code, 428)
        for i in range(3):
            self.confirm(self.quote(self.source(40)))
        self.assertEqual(self.quote(self.source(40))["lines"][0]["missing"], 40)

    def test_new_work_with_empty_balance_never_enters_queue(self):
        for _ in range(5):
            self.confirm(self.quote(self.source(20)))
        before = db().jobs.count_documents({"owner": self.owner})
        source = self.source(20)
        q = self.quote(source)
        self.assertEqual(q["lines"][0]["missing"], 20)
        with self.assertRaises(HTTPException) as short:
            api.confirm_quote(UUID(q["id"]), self.owner)
        self.assertEqual(short.exception.status_code, 402)
        with self.assertRaises(HTTPException) as direct:
            api.submit(api.Submission(url="https://www.instagram.com/reel/BetaReel/", request_id=uuid4()), self.owner)
        self.assertEqual(direct.exception.status_code, 428)
        self.assertEqual(db().jobs.count_documents({"owner": self.owner}), before)

    def test_reservation_must_cover_the_full_render(self):
        job = {"day": now().date().isoformat(), "source_file": ObjectId(), "quoted_seconds": 70}
        for reservation in ({}, {"slot": 0}, {"minute_units": ["one"] * 70}):
            with self.subTest(reservation=list(reservation)), self.assertRaises(HTTPException) as short:
                api.enqueue_render({**job, **reservation}, "free", 5)
            self.assertEqual(short.exception.status_code, 402)
        self.assertEqual(db().jobs.count_documents({"owner": self.owner}), 0)

    def test_monthly_credit_revoked_between_quote_and_confirm(self):
        purchase = self.pay(store.CREATOR)
        q = self.quote(self.source(90))
        purchase.revocationDate = purchase.signedDate + 1
        purchase.signedDate += 2
        store.record(purchase)
        with self.assertRaises(HTTPException) as changed:
            self.confirm(q)
        self.assertEqual(changed.exception.status_code, 409)
        self.assertEqual(db().jobs.count_documents({"owner": self.owner}), 0)

    def test_purchased_seconds_and_failed_target_release(self):
        tx = self.pay(store.MINUTE_PACK)
        store.record(tx, self.owner)
        q = self.quote(self.source(80), ("en", "es"))
        self.assertEqual(q["pack_left"], 740)
        self.assertEqual(api.me(self.owner)["pack_seconds"], 900)
        jobs = self.confirm(q)
        self.assertTrue(all(j["lane"] == "premium" for j in jobs))
        for job in jobs:
            self.assertEqual(len(db().jobs.find_one({"_id": job["id"]})["minute_units"]), 80)
        db().jobs.update_one(
            {"_id": jobs[1]["id"]},
            {"$set": {"state": "failed"}, "$unset": {"minute_units": "", "render_key": ""}},
        )
        db().jobs.update_one({"_id": jobs[0]["id"]}, {"$set": {"state": "done"}})
        api.delete_dub(jobs[0]["id"], self.owner)
        self.assertEqual(api.me(self.owner)["pack_seconds"], 820)
        tx.signedDate = 200
        tx.revocationDate = 200
        store.record(tx)
        self.assertEqual(api.me(self.owner)["pack_seconds"], 0)
        tx.signedDate = 300
        tx.revocationDate = None
        store.record(tx)
        self.assertEqual(api.me(self.owner)["pack_seconds"], 820)

    def test_monthly_rotation_paid_priority_and_shared_quote(self):
        self.pay(store.MINUTE_PACK)
        current = self.pay(store.CREATOR)
        source = self.source(120)
        q = self.quote(source, ("fr", "es"))
        self.assertEqual(q["monthly_left"], 6960)
        self.assertEqual(q["pack_left"], 900)
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: [j["id"] for j in self.confirm(q)], range(4)))
        self.assertTrue(all(x == ids[0] for x in ids))
        self.assertEqual(api.me(self.owner)["monthly_seconds"], 6960)
        replay = self.quote(source, ("fr", "es"))
        self.assertTrue(all(line["reused"] for line in replay["lines"]))
        current.signedDate = 250
        current.expiresDate = int((now() - timedelta(seconds=1)).timestamp() * 1000)
        store.record(current)
        self.assertEqual(api.me(self.owner)["monthly_seconds"], 0)
        self.pay(store.CREATOR)
        self.assertEqual(api.me(self.owner)["monthly_seconds"], 7200)
        self.assertEqual(api.me(self.owner)["pack_seconds"], 900)

    def test_competing_confirmations_and_erasure(self):
        self.pay(store.MINUTE_PACK)
        for _ in range(7):
            self.confirm(self.quote(self.source(100)))
        quotes = [self.quote(self.source(120)), self.quote(self.source(120))]

        def attempt(q):
            try:
                return self.confirm(q)[0]["id"]
            except HTTPException as exc:
                self.assertEqual(exc.status_code, 409)
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = [i for i in pool.map(attempt, quotes) if i]
        self.assertEqual(len(ids), 1)
        self.assertEqual(api.me(self.owner)["pack_seconds"], 80)
        from . import erasure

        db().jobs.update_many({"owner": self.owner}, {"$set": {"state": "done"}})
        erasure.erase(self.owner)
        db().accounts.insert_one({"_id": self.owner})
        erasure.restore_billing(self.owner)
        self.assertEqual(api.me(self.owner)["pack_seconds"], 80)

    def test_remaining_targets_are_reported_for_changed_balance(self):
        self.pay(store.MINUTE_PACK)
        for _ in range(7):
            self.confirm(self.quote(self.source(100)))
        source = self.source(80)
        quote = self.quote(source, ("en", "es"))
        self.confirm(self.quote(self.source(80)))
        from uuid import UUID

        response = api.confirm_quote(UUID(quote["id"]), self.owner)
        self.assertEqual([j["target_language"] for j in response["jobs"]], ["en"])
        self.assertEqual(response["remaining"], ["es"])
        self.assertEqual(api.me(self.owner)["pack_seconds"], 40)
        again = api.confirm_quote(UUID(quote["id"]), self.owner)
        self.assertEqual(again["jobs"][0]["id"], response["jobs"][0]["id"])

    def test_known_tokenless_renewal_is_owner_bound(self):
        tx = self.pay(store.CREATOR)
        tx.originalTransactionId = tx.transactionId
        tx.transactionId = str(uuid4())
        tx.appAccountToken = None
        with self.assertRaises(HTTPException):
            store.record(tx, "apple:other")
        self.assertIsNotNone(store.record(tx, self.owner))
        tx.originalTransactionId = str(uuid4())
        with self.assertRaises(HTTPException):
            store.record(tx, self.owner)
