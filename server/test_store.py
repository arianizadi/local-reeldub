"""Run after test_service in an isolated Compose database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from . import api, store
from .db import media_db, db, now
from .worker import claim


@unittest.skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1", "Requires disposable MongoDB")
class CommerceTests(unittest.TestCase):
    def setUp(self):
        self.user = self.account()
        self.paid = self.account("paid")

    def account(self, tier="free"):
        user = "apple:" + str(uuid4())
        db().accounts.insert_one({"_id": user, "tier": tier})
        return user

    def submit(self, user=None, request=None):
        return api.submit(api.Submission(url="https://www.instagram.com/reel/CheckoutFixture/", request_id=request or uuid4()), user or self.user)

    def tx(self, key="mini", user=None):
        return SimpleNamespace(productId=store.PREFIX + key, rawEnvironment="Production",
            signedDate=100, transactionId=str(uuid4()), quantity=1, revocationDate=None,
            isUpgraded=False, expiresDate=None, rawType="Consumable",
            rawInAppOwnershipType="PURCHASED", appAccountToken=store.store_token(user or self.user))

    def test_atomic_daily_and_purchased_spend(self):
        tx = self.tx()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: store.record(tx, self.user), range(12)))
        self.assertEqual(store.balances(self.user), {"queue_skips": 1, "extra_translations": 5})
        def attempt(_):
            try: return self.submit()
            except HTTPException as e:
                self.assertEqual(e.status_code, 429)
        with ThreadPoolExecutor(max_workers=12) as pool:
            jobs = [j for j in pool.map(attempt, range(18)) if j]
        self.assertEqual(len(jobs), 10)
        self.assertEqual(api.me(self.user)["used"], 5)
        self.assertEqual(store.balances(self.user)["extra_translations"], 0)
        spent = db().jobs.find_one({"owner": self.user, "extra_ticket": {"$exists": True}})
        db().jobs.update_one({"_id": spent["_id"]}, {"$set": {"state": "done"}})
        api.delete_dub(spent["_id"], self.user)
        self.assertEqual(store.balances(self.user)["extra_translations"], 0)
        with self.assertRaises(HTTPException):
            store.record(tx, self.paid)
        tx.signedDate = 200; tx.revocationDate = 200
        store.record(tx)
        tx.signedDate = 100; tx.revocationDate = None
        store.record(tx)
        self.assertEqual(store.balances(self.user)["queue_skips"], 0)

    def test_subscription_expiry_environment_and_verification(self):
        tx = self.tx("premium.monthly")
        tx.rawType = "Auto-Renewable Subscription"
        tx.expiresDate = int((now()+timedelta(days=30)).timestamp()*1000)
        tx.rawEnvironment = "Sandbox"
        store.record(tx)
        self.assertEqual(api.tier(self.user), "free")
        tx.rawEnvironment = "Production"
        store.record(tx)
        self.assertEqual(api.tier(self.user), "paid")
        tx.signedDate += 1; tx.expiresDate = int((now()-timedelta(seconds=1)).timestamp()*1000)
        store.record(tx)
        self.assertEqual(api.tier(self.user), "free")
        with patch.dict(os.environ, {"APPLE_CLIENT_ID":"com.arianizadi.reeldub", "STORE_APP_ID":"6836516034", "STORE_ENVIRONMENT":"Production"}):
            store.verifier.cache_clear()
            for n in (False, True):
                with self.assertRaises(HTTPException) as rejected: store.verify("invalid", notification=n)
                self.assertEqual(rejected.exception.status_code, 400)
            store.verifier.cache_clear()

    def test_fifo_promotion_and_retry(self):
        # Other test fixtures have their own queues. Isolate them from this worker claim.
        db().jobs.update_many({"state": {"$in":["queued","running"]}}, {"$set":{"state":"failed"}, "$unset":{"slot":""}})
        free_first = self.submit()
        boosted = self.submit()
        a = self.submit(self.paid)
        b = self.submit(self.account("paid"))
        store.record(self.tx("skip1"))
        with ThreadPoolExecutor(max_workers=4) as pool:
            promoted = list(pool.map(lambda _: api.skip(boosted["id"], self.user), range(4)))
        self.assertTrue(all(j["lane"] == "premium" for j in promoted))
        self.assertEqual(promoted[0]["ahead"], 2)
        self.assertEqual(api.job_status(free_first["id"], self.user)["ahead"], 3)
        self.assertEqual([claim()["_id"] for _ in range(4)], [a["id"], b["id"], boosted["id"], free_first["id"]])
        db().jobs.update_one({"_id": boosted["id"]}, {"$set":{"lease_end":now()-timedelta(seconds=1),"attempt":3}})
        claim()
        self.assertEqual(store.balances(self.user)["queue_skips"], 1)
        solo = self.submit()
        with self.assertRaises(HTTPException): api.skip(solo["id"], self.user)
        self.assertEqual(store.balances(self.user)["queue_skips"], 1)

    def test_refund_reversal_prorating_and_no_consumption_sharing(self):
        tx = self.tx("studio")
        tx.signedDate = 100
        key = store.record(tx, self.user)
        db().jobs.insert_one({"_id": str(uuid4()), "owner": self.user, "request_id": str(uuid4()),
                              "state": "done", "extra_ticket": key+"/29"})
        tx.signedDate = 200
        tx.rawRevocationType = "REFUND_PRORATED"
        tx.revocationPercentage = 50000
        tx.revocationDate = 200
        store.record(tx)
        self.assertEqual(store.balances(self.user), {"extra_translations": 14, "queue_skips": 2})
        tx.signedDate = 300
        tx.rawRevocationType = "REFUND_FULL"
        store.record(tx)
        self.assertEqual(store.balances(self.user), {"extra_translations": 0, "queue_skips": 0})
        tx.signedDate = 400
        tx.revocationDate = tx.rawRevocationType = tx.revocationPercentage = None
        store.record(tx)
        self.assertEqual(store.balances(self.user), {"extra_translations": 29, "queue_skips": 4})
        with patch.object(store, "verify", return_value=SimpleNamespace(rawNotificationType="CONSUMPTION_REQUEST")), patch.object(store, "record") as write:
            store.handle_notification("signed-refund-request-fixture")
            write.assert_not_called()

    def test_signed_grace_and_out_of_order_notifications(self):
        tx = self.tx("premium.monthly")
        tx.rawType = "Auto-Renewable Subscription"
        tx.originalTransactionId = tx.transactionId
        tx.expiresDate = int((now()-timedelta(minutes=15)).timestamp()*1000)
        data = SimpleNamespace(signedTransactionInfo="transaction", signedRenewalInfo="renewal", rawStatus=4)
        envelope = SimpleNamespace(rawNotificationType="DID_FAIL_TO_RENEW", data=data, signedDate=300)
        renewal = SimpleNamespace(originalTransactionId=tx.transactionId, productId=tx.productId,
                                  gracePeriodExpiresDate=int((now()+timedelta(days=1)).timestamp()*1000))
        items = {"event":envelope, "transaction":tx, "renewal":renewal}
        with patch.object(store, "verify", side_effect=lambda signed, **kwargs: items[signed]):
            store.handle_notification("event")
            self.assertTrue(store.premium(self.user))
            envelope.signedDate = 400; data.rawStatus = 2
            store.handle_notification("event")
            self.assertFalse(store.premium(self.user))
            envelope.signedDate = 350; data.rawStatus = 4
            store.handle_notification("event")
            self.assertFalse(store.premium(self.user))

    def test_missed_notification_recovery_retries_page(self):
        from . import store_history
        responses = [
            SimpleNamespace(notificationHistory=[SimpleNamespace(signedPayload="one")], hasMore=True, paginationToken="next"),
            RuntimeError("temporary service interruption"),
            SimpleNamespace(notificationHistory=[SimpleNamespace(signedPayload="two")], hasMore=False)]
        with patch.dict(os.environ, {"STORE_ENVIRONMENT":"Sandbox"}), patch.object(store_history,"client") as service, patch.object(store,"handle_notification") as apply:
            service.return_value.get_notification_history.side_effect = responses
            with self.assertRaises(RuntimeError):
                store_history.recover()
            journal = db().store_history
            self.assertEqual(journal.find_one({"_id":"Sandbox"})["page"], "next")
            journal.update_one({"_id":"Sandbox"}, {"$set":{"due": now()-timedelta(seconds=1)}})
            store_history.recover()
            self.assertEqual([call.args[0] for call in apply.call_args_list], ["one","two"])
            args = service.return_value.get_notification_history.call_args_list
            self.assertEqual(args[1].args[0], args[2].args[0])
            self.assertEqual(args[1].args[1].startDate, args[2].args[1].startDate)
            self.assertNotIn("page", journal.find_one({"_id":"Sandbox"}))

    def test_account_erasure_fresh_identity_and_billing_recovery(self):
        import gridfs
        from . import erasure
        tx = self.tx()
        store.record(tx, self.user)
        original_token = tx.appAccountToken
        job = self.submit()
        artifact = gridfs.GridFS(media_db()).put(b"private render")
        db().jobs.update_one({"_id":job["id"]}, {"$set":{"state":"done","artifact":artifact,"report":{"note":"private"}}})
        db().jobs.insert_one({"_id":str(uuid4()), "request_id":str(uuid4()), "owner":self.user, "state":"done",
                              "extra_ticket":f"Production:{tx.transactionId}/0", "url":"private"})
        body = api.DeleteAccount(identity_token="fixture",challenge="a"*32,authorization_code="fixture")
        with patch.object(api, "verify_identity", return_value={"sub":"other"}):
            with self.assertRaises(HTTPException): api.delete_account(body,self.user)
        self.assertTrue(gridfs.GridFS(media_db()).exists(artifact))
        with patch.object(api, "verify_identity", return_value={"sub": self.user.removeprefix("apple:")}), patch.dict(os.environ, {"APPLE_SIGNIN_CLIENT_SECRET_FILE":""}):
            self.assertFalse(api.delete_account(body,self.user)["apple_revoked"])
        erasure.resume()
        self.assertFalse(gridfs.GridFS(media_db()).exists(artifact))
        self.assertTrue(erasure.closing(self.user))
        self.assertIsNone(db().accounts.find_one({"_id":self.user}))
        self.assertFalse(db().jobs.count_documents({"owner":self.user}))
        tx.signedDate += 1; store.record(tx)
        retired_job = db().jobs.find_one({"owner":erasure.billing_id(self.user)})
        self.assertNotIn("url", retired_job)
        db().account_deletions.delete_one({"_id":self.user})
        db().accounts.insert_one({"_id":self.user})
        erasure.restore_billing(self.user)
        self.assertEqual(store.store_token(self.user),original_token)
        self.assertEqual(store.balances(self.user),{"extra_translations":4,"queue_skips":1})

    def test_language_cache_and_legacy_idempotency(self):
        from .source import reel_url
        url = reel_url("https://instagram.com/reel/LanguageTest/")
        def submit(target, request_id=None):
            return api.submit(api.Submission(url=url,target_language=target,request_id=request_id or uuid4()),self.user)
        with patch.dict(os.environ, {"REELDUB_OUTPUT_PROFILE":"studio"}):
            english, spanish = submit("en"), submit("es")
            # Simulate a pre-language render in the original user's library.
            db().jobs.update_one({"_id":english["id"]}, {"$set":{"artifact":"legacy","state":"done"}, "$unset":{"target_language":""}})
            db().jobs.update_one({"_id":spanish["id"]}, {"$set":{"artifact":"spanish","state":"done"}})
            with patch.object(api.sources, "prepare_reel", return_value={"seconds": 30}) as inspect_source:
                self.assertEqual(submit("en")["id"], english["id"])
                self.assertEqual(submit("es")["id"], spanish["id"])
                self.assertEqual(inspect_source.call_count, 2)
                self.assertEqual(submit("en")["id"], english["id"])
                self.assertEqual(inspect_source.call_count, 2)
            german_key = uuid4()
            german = submit("de", german_key)
            self.assertEqual(german["target_language"], "de")
            with self.assertRaises(HTTPException) as mismatch: submit("es", german_key)
            self.assertEqual(mismatch.exception.status_code, 409)
            self.assertEqual(api.me(self.user)["used"], 3)
            with self.assertRaises(HTTPException): submit("xx")
        with patch.dict(os.environ, {"REELDUB_OUTPUT_PROFILE":"light"}):
            self.assertEqual(api.me(self.user)["output_languages"], ["en"])
            with self.assertRaises(HTTPException): submit("es")
