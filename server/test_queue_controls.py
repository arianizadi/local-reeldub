"""Run against a disposable ReelDub database, with no other workers."""
import os
from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase, skipUnless
from uuid import UUID, uuid4

from fastapi import HTTPException

from . import api, worker
from .db import db, now, setup_indexes


@skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1", "Requires disposable database")
class QueueControls(TestCase):
    @classmethod
    def setUpClass(cls):
        setup_indexes()

    def setUp(self):
        self.owner = "queue-test-" + uuid4().hex
        self.addCleanup(db().jobs.delete_many, {"owner": self.owner})

    def job(self, state="queued", **fields):
        ident = uuid4()
        db().jobs.insert_one(dict(_id=str(ident), owner=self.owner, request_id=uuid4().hex,
            url="https://example.com/movie", created=now(), priority=0, queue_seq=api.next_position(),
            state=state, stage=state, attempt=0, **fields))
        return ident

    def move(self, job, position):
        return api.move_job(job, api.QueueMove(position=position), self.owner)

    def test_moves_change_claim_order_and_leave_future_submissions_behind(self):
        first = self.job()
        second = self.job()
        recovering = self.job(recovery_rank=1)
        self.assertEqual(self.move(second, "first")["ahead"], 0)
        self.assertEqual(self.move(first, "first")["ahead"], 0)
        self.assertEqual(api.job_status(str(second), self.owner)["ahead"], 1)
        self.assertEqual(self.move(recovering, "last")["ahead"], 2)
        last = self.job()
        for expected in (first, second, recovering, last):
            self.assertEqual(worker.claim(studio=True)["_id"], str(expected))

    def test_cancel_revokes_running_lease_and_blocks_late_publish_and_requeue(self):
        ident = self.job()
        job = worker.claim(studio=True)
        fence = {"_id": str(ident), "state": "running", "lease": job["lease"]}
        cancelled = api.cancel_job(ident, self.owner)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertFalse(db().jobs.update_one(fence, {"$set": {"state": "done"}}).matched_count)
        self.assertEqual(api.cancel_job(ident, self.owner)["finished"], cancelled["finished"])
        self.assertIsNone(worker.claim(studio=True))
        self.assertFalse(cancelled["available_on_server"])
        self.assertIsNone(db().jobs.find_one({"_id": str(ident)}).get("fleet_slot"))

    def test_cancel_and_move_never_change_another_library_or_terminal_job(self):
        ident = self.job()
        for operation in [lambda: api.cancel_job(ident, "other"),
                          lambda: api.move_job(ident, api.QueueMove(position="last"), "other")]:
            with self.assertRaises(HTTPException) as ex:
                operation()
            self.assertEqual(ex.exception.status_code, 404)
        api.cancel_job(ident, self.owner)
        with self.assertRaises(HTTPException) as ex:
            self.move(ident, "first")
        self.assertEqual(ex.exception.status_code, 409)
        api.delete_dub(str(ident), self.owner, revision=None)
        self.assertTrue(db().jobs.find_one({"_id": str(ident)})["deleted"])
        done = self.job(state="done")
        with self.assertRaises(HTTPException) as ex:
            api.cancel_job(done, self.owner)
        self.assertEqual(ex.exception.status_code, 409)

    def test_two_devices_moving_jobs_do_not_drop_either_job(self):
        ids = [self.job(), self.job()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda ident: self.move(ident, "first"), ids))
        self.assertEqual(sorted(api.job_status(str(i), self.owner)["ahead"] for i in ids), [0, 1])
        self.assertEqual({UUID(worker.claim(studio=True)["_id"]) for _ in ids}, set(ids))
