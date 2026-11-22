"""Real-storage regression for quota and priority measurements on isolated PG/Mongo fixtures."""
import os
from datetime import UTC, datetime, timedelta
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from . import api, launch_metrics
from .db import db, setup_indexes, uses_postgres


@skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1", "Disposable test database required")
class LaunchMetricsTests(TestCase):
    def setUp(self):
        if uses_postgres():
            from .postgres import install
            with db().connection() as conn:
                install(conn)
        else:
            setup_indexes()
        self.stamp = datetime(2003, 3, 1, 12, tzinfo=UTC)
        self.owner = "apple:" + uuid4().hex
        self.tag = uuid4().hex
        self.ids = []
        self.environment = patch.dict(os.environ, {
            "REELDUB_CREATOR_MINUTES": "0", "REELDUB_FLEET_REGISTRY":
            '{"nodes":{"studio":{"enabled":true,"studio":true,"slots":2},'
            '"light":{"slots":1},"disabled":{"enabled":false,"slots":1,"studio":true}}}'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(lambda: db().jobs.delete_many({"_id": {"$in": self.ids}}))

    def job(self, owner, stamp, **fields):
        key = str(uuid4())
        self.ids.append(key)
        return {"_id": key, "owner": owner, "request_id": key, "day": stamp.date().isoformat(),
                "url": "https://www.instagram.com/reel/ABCDE/", "target_language": "en",
                "stage": "Queued", "state": "queued", "created": stamp, "attempt": 0, "priority": 0,
                "queue_seq": 200, **fields}

    def admit(self, owner, plan, limit, stamp, **fields):
        job = self.job(owner, stamp, **fields)
        with patch.object(api, "now", return_value=stamp), patch.object(api, "notify", return_value=True):
            api.enqueue_render(job, plan, limit)
        return job["_id"]

    def test_real_admissions_and_tombstones(self):
        stamp = self.stamp
        # UTC window includes 29 days ago, including the whole first UTC day.
        oldest = (stamp - timedelta(days=29)).replace(hour=0)
        self.admit(self.owner, "free", 5, oldest, slot=0)
        self.admit(self.owner, "free", 5, oldest - timedelta(seconds=1), slot=0)
        for slot in range(5):
            last = self.admit(self.owner, "free", 5, stamp, slot=slot)
        # Terminal failure released slot 4. Reusing the slot doesn't count a second hit.
        db().jobs.update_one({"_id": last}, {"$set": {"state": "failed"}, "$unset": {"slot": ""}})
        api.delete_dub(last, self.owner)
        self.admit(self.owner, "free", 5, stamp, slot=4)
        self.admit(self.owner + "-second", "free", 5, stamp, slot=1)
        self.admit(self.owner + "-grant", "free", 50, stamp, slot=4)
        self.admit(api.BETA_REVIEWER, "free", 5, stamp, slot=4)
        self.admit(self.owner + "-admin", "admin", None, stamp, priority=1)
        self.admit(self.owner, "free", 5, stamp, extra_ticket=self.tag)
        old = self.job(self.owner + "-legacy", stamp, priority=1, slot=4, started=stamp)
        db().jobs.insert_one(old)
        data = launch_metrics.overview(stamp)
        self.assertEqual((data["free_active_users"], data["free_limit_users"], data["free_active_user_days"],
                          data["free_limit_user_days"]), (2, 1, 3, 1))
        self.assertEqual(data["priority_started"], 0)
        self.assertIsNone(data["busy_priority_p95_seconds"])
        self.assertIsNone(data["priority_median_seconds"])

    def test_priority_wait_and_compatible_live_slots(self):
        stamp = self.stamp
        occupied = []
        for slot in ("studio:0", "studio:1"):
            row = self.job(self.owner, stamp, state="running", fleet_slot=slot, lease_end=stamp+timedelta(minutes=2))
            db().jobs.insert_one(row)
            occupied.append(row["_id"])
        busy = self.admit(self.owner, "paid", 50, stamp, slot=0, render_quality="best", priority=1)
        explicit = launch_metrics.priority_entry({"target_language": "en"}, stamp)
        self.assertFalse(explicit.get("launch_busy"))  # Light worker also eligible, and idle.
        db().jobs.update_one({"_id": occupied[0]}, {"$set": {"lease_end": stamp}})
        idle = self.admit(self.owner, "paid", 50, stamp, slot=1, render_quality="best", priority=1)
        self.admit(self.owner, "paid", 50, stamp, slot=2, render_quality="best", priority=1)  # still waiting
        for key, seconds in ((busy, 12), (idle, 2)):
            db().jobs.update_one({"_id": key}, {"$set": {"state": "done", "started": stamp+timedelta(seconds=seconds)}})
        api.delete_dub(busy, self.owner)
        data = launch_metrics.overview(stamp + timedelta(minutes=1))
        self.assertEqual(data["priority_started"], 2)
        self.assertEqual(data["priority_median_seconds"], 7)
        self.assertAlmostEqual(data["priority_p95_seconds"], 11.5)
        self.assertEqual(data["busy_priority_started"], int(uses_postgres()))
        self.assertEqual(data["priority_occupancy_known"], 2 if uses_postgres() else 0)
        self.assertEqual(data["busy_priority_p95_seconds"], 12 if uses_postgres() else None)

    def test_paid_skip_wait_begins_at_promotion(self):
        stamp = self.stamp
        previous = self.job(self.owner + "-other", stamp, queue_seq=1)
        db().jobs.insert_one(previous)
        job = self.admit(self.owner, "free", 5, stamp, slot=0)
        promoted = stamp + timedelta(seconds=40)
        with patch.object(api, "now", return_value=promoted), patch.object(api, "limit_writes"), \
             patch.object(api.store, "tickets", return_value=[self.tag]), patch.object(api, "next_position", return_value=201):
            api.skip(job, self.owner)
            api.skip(job, self.owner)  # Replay keeps the original entry time.
        db().jobs.update_one({"_id": job}, {"$set": {"state":"done", "started":stamp+timedelta(seconds=45)}})
        result = launch_metrics.overview(promoted+timedelta(seconds=10))
        self.assertEqual(result["priority_started"], 1)
        self.assertEqual(result["priority_median_seconds"], 5)
