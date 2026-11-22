"""PostgreSQL claim integration tests. Run against a disposable, empty job queue."""
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from . import worker
from .db import db, now, uses_postgres
from .postgres import install


def _start_node(root, barrier):
    # Each spawned process sees its own installation and opens its own SQL pool.
    global _claim_barrier
    _claim_barrier = barrier
    worker.__file__ = str(root / "server" / "worker.py")
    db().command("ping")


def _simultaneous_claim():
    _claim_barrier.wait(timeout=15)
    return worker.claim()


@unittest.skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1" and uses_postgres(),
                     "Requires a disposable PostgreSQL database")
class TwoServerQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with db().connection() as conn:
            install(conn)

    def setUp(self):
        self.jobs = db().jobs
        self.assertEqual(self.jobs.count_documents({"state": {"$in": ["queued", "running"]}}),
                         0, "Use a disposable queue with no other workers or pending jobs")
        self.owners = ["scheduler-test:" + str(uuid4()) for _ in range(2)]
        self.addCleanup(self.jobs.delete_many, {"owner": {"$in": self.owners}})
        self.context = multiprocessing.get_context("spawn")
        self.barrier = self.context.Barrier(2)

    def node(self, *, studio=True):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        if studio:
            marker = root / ".qwen-venv" / "ready"
            marker.parent.mkdir()
            marker.touch()
        pool = ProcessPoolExecutor(1, mp_context=self.context, initializer=_start_node,
                                   initargs=(root, self.barrier))
        self.addCleanup(pool.shutdown)
        return pool

    def queue(self, seq, priority=0, **fields):
        job = {"_id": str(uuid4()), "owner": self.owners[0], "request_id": str(uuid4()),
               "state": "queued", "queue_seq": seq, "priority": priority, "created": now(), "day": "2026-01-01",
               "attempt": 0, **fields}
        self.jobs.insert_one(job)
        return job["_id"]

    def race(self, first, second):
        pending = [node.submit(_simultaneous_claim) for node in (first, second)]
        return [future.result(timeout=25) for future in pending]

    def test_fifo_in_each_lane_and_exactly_one_job_per_claim(self):
        first, second = self.node(), self.node()
        # Insert in reverse to make insertion order differ from the two FIFO lanes.
        jobs = {seq: self.queue(seq, priority=int(seq in {2, 4, 5}), owner=self.owners[seq % 2])
                for seq in range(7, 0, -1)}
        leases = set()
        for positions in ((2, 4), (5, 1), (3, 6), (7,), ()):
            batch = [job for job in self.race(first, second) if job is not None]
            self.assertCountEqual([job["_id"] for job in batch], [jobs[n] for n in positions])
            for job in batch:
                self.assertEqual(job["attempt"], 1)
                self.assertEqual(job["state"], "running")
                self.assertNotIn(job["lease"], leases)
                leases.add(job["lease"])
        self.assertEqual(len(leases), 7)

    def test_recovery_priority_matches_detail_and_library_positions(self):
        from .api import summary
        paid = self.queue(1, priority=1)
        regular = self.queue(2)
        interrupted = self.queue(9, recovery_rank=1, recovery_count=1, recovery_from_node="first")
        for job_id, ahead in ((interrupted, 0), (paid, 1), (regular, 2)):
            self.jobs.update_one({"_id": job_id}, {"$set": {"url": "https://www.instagram.com/reel/TestABC/",
                                                           "stage": "Queued"}})
            self.assertEqual(summary(self.jobs.find_one({"_id": job_id}))["ahead"], ahead)
        self.assertEqual({row["_id"]: ahead for row, ahead in db().library(self.owners[0])},
                         {interrupted: 0, paid: 1, regular: 2})
        claimed = worker.claim(studio=True, fleet_slot="second:0", fleet_request=str(uuid4()))
        self.assertEqual(claimed["_id"], interrupted)
        self.assertTrue(summary(claimed)["recovery_switched_server"])
        self.assertEqual(worker.claim(studio=True)["_id"], paid)

    def test_worker_can_claim_second_priority_job_while_first_is_locked(self):
        node = self.node()
        free = self.queue(1)
        first = self.queue(2, priority=1)
        second = self.queue(3, priority=1, owner=self.owners[1])
        with db().connection() as conn:
            # Hold the first claimant's row lock open until the other claimant returns.
            conn.execute("SELECT id FROM reeldub.jobs WHERE doc->>'_id'=%s FOR UPDATE", (first,))
            self.assertEqual(node.submit(worker.claim).result(timeout=15)["_id"], second)
        for expected in (first, free):
            self.assertEqual(node.submit(worker.claim).result(timeout=15)["_id"], expected)

    def test_light_node_leaves_incompatible_jobs_for_studio(self):
        light, studio = self.node(studio=False), self.node()
        best = self.queue(1, priority=1, render_quality="best", target_language="en")
        experiment = self.queue(2, priority=1, lab_mode="medium")
        russian = self.queue(3, priority=1, target_language="ru")
        legacy_english = self.queue(4)
        light_job, studio_job = self.race(light, studio)
        self.assertEqual((light_job["_id"], studio_job["_id"]), (legacy_english, best))
        self.assertIsNone(light.submit(worker.claim).result(timeout=20))
        for expected in (experiment, russian):
            self.assertEqual(studio.submit(worker.claim).result(timeout=20)["_id"], expected)

    def test_worker_loss_retries_only_after_expiry_and_fences_old_worker(self):
        first, survivor = self.node(), self.node()
        key = self.queue(1, render_quality="best")
        claimed = first.submit(worker.claim).result(timeout=20)
        first.shutdown()  # A worker exits without finishing or returning its leased job.
        self.assertIsNone(survivor.submit(worker.claim).result(timeout=20))
        old_fence = {"_id": key, "lease": claimed["lease"], "state": "running"}
        self.assertEqual(self.jobs.update_one(old_fence, {
            "$set": {"lease_end": now() - timedelta(seconds=1)}}).matched_count, 1)
        retry = survivor.submit(worker.claim).result(timeout=20)
        self.assertEqual(retry["_id"], key)
        self.assertEqual(retry["attempt"], 2)
        self.assertEqual(retry["started"], claimed["started"])
        self.assertNotEqual(retry["lease"], claimed["lease"])
        self.assertGreater(retry["lease_end"], now())
        self.assertFalse(worker.RenderLease(self.jobs, old_fence, claimed["lease_end"]).renew({"stage": "Voicing"}))
        finish = {"$set": {"state": "done"}}
        self.assertEqual(self.jobs.update_one(old_fence, finish).matched_count, 0)
        self.assertEqual(self.jobs.update_one({**old_fence, "lease": retry["lease"]}, finish).matched_count, 1)

    def test_exhausted_job_releases_reservations_while_workers_claim_next(self):
        first, second = self.node(), self.node()
        fields = {"slot": 1, "minute_grants": ["grant"], "minute_units": ["grant:1"],
                  "render_key": "expired-render", "extra_ticket": "extra", "skip_ticket": "skip"}
        expired = self.queue(1, priority=1, state="running", lease=str(uuid4()),
                             lease_end=now() - timedelta(seconds=1), attempt=worker.MAX_ATTEMPTS,
                             **fields)
        next_job = self.queue(2)
        batch = [job for job in self.race(first, second) if job is not None]
        self.assertEqual([job["_id"] for job in batch], [next_job])
        failed = self.jobs.find_one({"_id": expired})
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["attempt"], worker.MAX_ATTEMPTS)
        self.assertTrue(failed["finished"])
        self.assertFalse((fields.keys() | {"lease", "lease_end"}) & failed.keys())
        # Reuse every released unique key, including trigger-maintained minute units.
        replacement = self.queue(3, **fields)
        self.assertEqual(first.submit(worker.claim).result(timeout=20)["_id"], replacement)
