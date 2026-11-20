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
        source = self.source(480)
        q = self.quote(source, ("de", "es"))
        self.assertEqual(q["monthly_left"], 6240)
        self.assertEqual(q["pack_left"], 900)
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: [j["id"] for j in self.confirm(q)], range(4)))
        self.assertTrue(all(x == ids[0] for x in ids))
        self.assertEqual(api.me(self.owner)["monthly_seconds"], 6240)
        replay = self.quote(source, ("de", "es"))
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
        quotes = [self.quote(self.source(600)), self.quote(self.source(600))]

        def attempt(q):
            try:
                return self.confirm(q)[0]["id"]
            except HTTPException as exc:
                self.assertEqual(exc.status_code, 409)
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = [i for i in pool.map(attempt, quotes) if i]
        self.assertEqual(len(ids), 1)
        self.assertEqual(api.me(self.owner)["pack_seconds"], 300)
        from . import erasure

        db().jobs.update_one({"_id": ids[0]}, {"$set": {"state": "done"}})
        erasure.erase(self.owner)
        db().accounts.insert_one({"_id": self.owner})
        erasure.restore_billing(self.owner)
        self.assertEqual(api.me(self.owner)["pack_seconds"], 300)

    def test_remaining_targets_are_reported_for_changed_balance(self):
        self.pay(store.MINUTE_PACK)
        source = self.source(400)
        quote = self.quote(source, ("en", "es"))
        self.confirm(self.quote(self.source(200)))
        from uuid import UUID

        response = api.confirm_quote(UUID(quote["id"]), self.owner)
        self.assertEqual([j["target_language"] for j in response["jobs"]], ["en"])
        self.assertEqual(response["remaining"], ["es"])
        self.assertEqual(api.me(self.owner)["pack_seconds"], 300)
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
