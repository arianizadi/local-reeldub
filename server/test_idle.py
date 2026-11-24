"""Queue, sign-in and maintenance behavior on an isolated metadata database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import select
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException

from . import api, store_history, wakeup
from .db import db, media_db, now, uses_postgres


class LocalWakeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Linux notification")
    def test_one_signal_wakes_every_waiter_and_idle_reads_no_queue(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"REELDUB_WAKE_FILE": str(Path(temp) / "signal")}), patch.object(wakeup, "separate_metadata", return_value=False):
            workers = [wakeup.Wake(), wakeup.Wake()]
            for worker in workers:
                self.assertIsNotNone(worker.watcher)
                self.addCleanup(worker.watcher.close)
                worker.watcher.read(timeout=0)
            wakeup.notify()
            for worker in workers:
                self.assertTrue(select.select([worker.watcher], [], [], 1)[0])
                worker.wait()
                self.assertFalse(select.select([worker.watcher], [], [], 0)[0])
                with patch.object(worker.watcher, "read", return_value=[]) as read:
                    worker.wait()
                    read.assert_called_once_with(timeout=60000)


@unittest.skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1" and uses_postgres(), "Disposable Postgres only")
class IdleTests(unittest.TestCase):
    def test_progress_checks_revocation_in_one_auth_lookup(self):
        token, user, job = str(uuid4()), "apple:" + str(uuid4()), uuid4()
        database = db()
        digest = api.hash_value(token)
        self.addCleanup(database.sessions.delete_one, {"_id": digest})
        self.addCleanup(database.account_deletions.delete_one, {"_id": user})
        database.sessions.insert_one({"_id": digest, "owner": user, "expires": now() + timedelta(minutes=2)})
        with patch.object(database, "connection", wraps=database.connection) as checkouts:
            self.assertEqual(api.library_progress(api.LibraryCheck(ids=[job]), api.owner("Bearer " + token)), [])
            self.assertEqual(checkouts.call_count, 2)
            checkouts.reset_mock()
            self.assertEqual(api.me(api.owner("Bearer " + token), "")["tier"], "free")
            self.assertEqual(checkouts.call_count, 2)
        database.account_deletions.insert_one({"_id": user})
        with self.assertRaises(HTTPException) as denied:
            api.owner("Bearer " + token)
        self.assertEqual(denied.exception.status_code, 401)

    def test_store_history_stays_on_half_hour_boundaries(self):
        stamp = now().replace(hour=23, minute=14, second=39)
        half = stamp.replace(minute=30, second=0, microsecond=0)
        self.assertEqual(store_history.next_check(stamp), half)
        self.assertEqual(store_history.next_check(half + timedelta(seconds=8)), wakeup.next_hour(half))

    def test_only_one_replica_runs_a_due_sweep(self):
        schedule = media_db().maintenance
        schedule.delete_one({"_id": "postgres-hourly"})
        with patch.object(api, "resume_deletions") as cleanup, patch.object(api, "purge_orphaned_uploads") as purge:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: api.maintain_postgres(), range(4)))
            delay = api.maintain_postgres()
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 3600)
            cleanup.assert_called_once()
            purge.assert_called_once()
        self.assertGreater(schedule.find_one({"_id": "postgres-hourly"})["due"], now())

    def test_worker_wakes_for_insert_and_recovers_if_final_signal_is_lost(self):
        worker = wakeup.Wake()
        stamp = now().replace(minute=10)
        with patch.object(wakeup, "now", return_value=stamp), patch.object(wakeup.time, "monotonic") as clock:
            clock.return_value = 30
            self.assertTrue(worker.due())
            self.assertFalse(worker.due())
            wakeup.notify(preparing=True)
            self.assertTrue(worker.due())
            clock.return_value += 5
            self.assertTrue(worker.due())
            self.assertFalse(worker.due())
            wakeup.notify()
            self.assertTrue(worker.due())
        with patch.object(wakeup, "now", return_value=stamp + timedelta(minutes=4)), patch.object(wakeup.time, "monotonic", return_value=300):
            self.assertTrue(worker.due())
            self.assertFalse(worker.due())

    def test_no_reservation_when_queue_cannot_receive_its_wake_signal(self):
        job = {"_id": str(uuid4()), "day": now().date().isoformat()}
        with patch.object(api, "notify", return_value=False), self.assertRaises(HTTPException) as failure:
            api.enqueue_render(job, "admin", None)
        self.assertEqual(failure.exception.status_code, 503)
        self.assertIsNone(db().jobs.find_one({"_id": job["_id"]}))

    def test_completed_admission_skips_recovery_polls_but_overlap_retains_them(self):
        signals = media_db().queue_wake
        signals.delete_one({"_id": "gpu"})
        worker = wakeup.Wake()
        first = wakeup.notify(preparing=True)
        self.assertTrue(worker.due())
        wakeup.notify(ticket=first)
        self.assertTrue(worker.due())
        self.assertIsNone(worker.settle_until)
        first = wakeup.notify(preparing=True)
        second = wakeup.notify(preparing=True)
        wakeup.notify(ticket=second)
        wakeup.notify(ticket=first)
        self.assertTrue(worker.due())
        self.assertGreater(worker.settle_until, now())

    def test_anonymous_challenge_uses_local_ttl_and_can_be_consumed_only_once(self):
        req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
        with patch.object(api, "db", side_effect=AssertionError("unexpected metadata connection")):
            challenge = api.challenge(req)["challenge"]
            login = api.AppleLogin(challenge=challenge, identity_token="sample")
            claims = {"nonce": api.hash_value(challenge), "sub": "tester"}
            with patch.object(api.apple_keys, "get_signing_key_from_jwt"), patch.object(api.jwt, "decode", return_value=claims):
                self.assertEqual(api.verify_identity(login), claims)
                with self.assertRaises(HTTPException) as rejected:
                    api.verify_identity(login)
                self.assertEqual(rejected.exception.status_code, 401)

    def test_library_positions_match_single_job_positions_across_owners(self):
        user, other = ("apple:" + str(uuid4()) for _ in range(2))
        self.addCleanup(db().jobs.delete_many, {"owner": {"$in": [user, other]}})
        for owner, priority, seq in [(user, 0, 500), (other, 1, 700), (user, 1, None), (other, 0, None), (user, 1, 700)]:
            job = {"_id": str(uuid4()), "owner": owner, "request_id": str(uuid4()), "url": "example",
                   "priority": priority, "attempt": 0, "state": "queued", "stage": "Queued", "created": now()}
            if seq is not None:
                job["queue_seq"] = seq
            db().jobs.insert_one(job)
        feed = db().library(user)
        self.assertEqual(len(feed), 3)
        for row, position in feed:
            self.assertEqual(row["owner"], user)
            self.assertEqual(position, api.summary(row)["ahead"])
        selected, _ = feed[0]
        other_job = db().jobs.find_one({"owner": other})
        ids = api.LibraryCheck(ids=[selected["_id"], other_job["_id"], uuid4()])
        self.assertEqual(api.library_progress(ids, user), [api.summary(selected)])
        db().jobs.update_one({"_id": selected["_id"]}, {"$set": {"deleted": True}})
        self.assertEqual(api.library_progress(ids, user), [])
