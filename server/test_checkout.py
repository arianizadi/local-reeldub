"""Only exercise simulated storage failure or read-only mode in disposable databases."""

import os
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from psycopg.errors import ReadOnlySqlTransaction

from . import api, checkout, store
from .db import db, uses_postgres


@skipUnless(
    os.getenv("REELDUB_TEST_DATABASE") == "1" and uses_postgres(),
    "Requires disposable Postgres",
)
class CheckoutTests(TestCase):
    def setUp(self):
        self.user = "apple:" + str(uuid4())
        self.config = patch.dict(
            os.environ,
            {
                "STORE_ENVIRONMENT": "Sandbox",
                "REELDUB_CREATOR_MINUTES": "1",
                "REELDUB_STORE_STORAGE_LIMIT_BYTES": "1000000000",
            },
        )
        self.config.start()
        self.addCleanup(self.config.stop)
        db().accounts.insert_one({"_id": self.user})
        self.token = store.store_token(self.user)

    def test_admission_commits_and_capacity_refuses(self):
        intent = checkout.prepare(self.user, store.MINUTE_PACK)
        row = db().purchase_intents.find_one({"_id": intent["id"]})
        self.assertEqual(
            (row["owner"], row["account"], row["product"]),
            (self.user, self.token, store.MINUTE_PACK),
        )
        with (
            patch.dict(os.environ, {"REELDUB_STORE_STORAGE_LIMIT_BYTES": "1"}),
            self.assertRaises(HTTPException) as caught,
        ):
            checkout.prepare(self.user, store.MINUTE_PACK)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(db().purchase_intents.count_documents({"owner": self.user}), 1)

    def test_self_host_intent_without_cloud_account_is_separate_and_fails_closed(self):
        app_proof = SimpleNamespace(appTransactionId=str(uuid4()), rawReceiptType="Sandbox")
        body = api.SelfHostAppProof(signed_app="fixture")
        with patch.object(store, "verify_self_host", return_value=app_proof), patch.dict(os.environ, {"REELDUB_CREATOR_MINUTES":"0", "STORE_ENVIRONMENT":""}):
            intent = api.prepare_self_host(body)
            self.assertEqual(intent, {"id":intent["id"], "account":intent["id"], "product":store.SELF_HOST})
            row = db().purchase_intents.find_one({"_id":intent["id"]})
            self.assertEqual(row["environment"], "Sandbox")
            self.assertTrue(row["owner"].startswith("selfhost:"))
            self.assertIsNone(db().accounts.find_one({"_id":row["owner"]}))
            with patch.dict(os.environ, {"REELDUB_STORE_STORAGE_LIMIT_BYTES":"1"}), self.assertRaises(HTTPException) as paused:
                api.prepare_self_host(body)
            self.assertEqual(paused.exception.status_code, 503)
            self.assertEqual(db().purchase_intents.count_documents({"owner":row["owner"]}), 1)

    def test_self_host_sale_replay_and_refund_never_grant_cloud_balance(self):
        intent = checkout.commit_intent("selfhost:test", store.SELF_HOST, "Sandbox")
        tx = SimpleNamespace(productId=store.SELF_HOST, rawEnvironment="Sandbox", rawType="Non-Consumable",
                             rawInAppOwnershipType="PURCHASED", quantity=1, transactionId=str(uuid4()), originalTransactionId=None,
                             signedDate=100, revocationDate=None, appAccountToken=intent["id"])
        key = "Sandbox:" + tx.transactionId
        with patch.object(store, "verify_self_host", return_value=tx), patch.dict(os.environ, {"REELDUB_STORE_STORAGE_LIMIT_BYTES":"1"}):
            body = api.SignedTransaction(signed_transaction="fixture")
            api.deliver_self_host(body)
            api.deliver_self_host(body)
            self.assertEqual(db().purchase_intents.find_one({"_id":intent["id"]})["transaction"], key)
            tx.signedDate, tx.revocationDate = 102, 102
            api.deliver_self_host(body)
            tx.signedDate, tx.revocationDate = 100, None
            api.deliver_self_host(body)
            record = db().purchases.find_one({"_id":key})
            self.assertTrue(record["revoked"])
            self.assertNotIn("seconds", record)
            self.assertNotIn("credits", record)
            self.assertNotIn("owner", record)
            tx.productId = store.MINUTE_PACK
            with self.assertRaises(HTTPException):
                api.deliver_self_host(body)

    def test_no_new_sales_when_creator_minutes_are_paused(self):
        with patch.dict(os.environ, {"REELDUB_CREATOR_MINUTES": "0"}):
            with self.assertRaises(HTTPException) as caught:
                checkout.prepare(self.user, store.MINUTE_PACK)
            self.assertEqual(caught.exception.status_code, 503)
            account = api.me(self.user, "1")
            self.assertFalse(account["purchase_preflight"])
            self.assertEqual(account["store_token"], self.token)
            self.assertEqual(db().purchase_intents.count_documents({"owner": self.user}), 0)

    def test_failed_database_does_not_admit(self):
        with (
            patch.object(db(), "connection", side_effect=ReadOnlySqlTransaction()),
            self.assertRaises(HTTPException) as failure,
        ):
            checkout.prepare(self.user, store.MINUTE_PACK)
        self.assertEqual(failure.exception.status_code, 503)
        self.assertEqual(db().purchase_intents.count_documents({"owner": self.user}), 0)

    def test_paid_redelivery_independent_of_capacity_and_intent(self):
        intent = checkout.prepare(self.user, store.MINUTE_PACK)
        transaction = SimpleNamespace(
            productId=store.MINUTE_PACK,
            originalTransactionId=None,
            transactionId=str(uuid4()),
            appAccountToken=self.token,
            rawEnvironment="Sandbox",
            rawType="Consumable",
            rawInAppOwnershipType="PURCHASED",
            signedDate=100,
            quantity=1,
            expiresDate=None,
            revocationDate=None,
            isUpgraded=False,
        )
        with (
            patch.dict(os.environ, {"REELDUB_STORE_STORAGE_LIMIT_BYTES": "1"}),
            patch.object(store, "verify", return_value=transaction),
        ):
            api.purchase(
                api.SignedTransaction(signed_transaction="signed", intent=intent["id"]),
                self.user,
            )
            api.purchase(api.SignedTransaction(signed_transaction="signed"), self.user)
        self.assertEqual(db().purchases.count_documents({"owner": self.user}), 1)
        self.assertEqual(db().purchases.find_one({"owner": self.user})["seconds"], 900)
        self.assertTrue(
            db().purchase_intents.find_one({"_id": intent["id"]})["transaction"]
        )
