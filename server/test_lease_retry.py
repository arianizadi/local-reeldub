from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from psycopg import OperationalError
from psycopg_pool import PoolTimeout
from pymongo.errors import AutoReconnect

from . import worker


class LeaseRetryTests(TestCase):
    def setUp(self):
        self.stamp = datetime(2026, 1, 1, tzinfo=UTC)
        self.tick = 0
        self.enterContext(patch.object(worker.time, "monotonic", side_effect=lambda: self.tick))
        self.enterContext(patch.object(worker, "now", side_effect=lambda: self.stamp + timedelta(seconds=self.tick)))
        self.jobs = Mock()
        self.fence = {"_id": "test", "lease": "first", "state": "running"}

    def lease(self, seconds=90):
        lease = worker.RenderLease(self.jobs, self.fence, self.stamp + timedelta(seconds=seconds))

        def advance(seconds):
            self.tick += seconds

        self.enterContext(patch.object(lease.stop, "wait", side_effect=advance))
        return lease

    def test_temporary_disconnect_recovers_same_progress_with_bounded_backoff(self):
        lease = self.lease()
        self.jobs.update_one.side_effect = [OperationalError(), AutoReconnect(), PoolTimeout(),
                                            Mock(matched_count=1)]
        self.assertTrue(lease.renew({"stage": "Voicing"}))
        self.assertEqual([call.args[0] for call in lease.stop.wait.call_args_list], [1, 2, 4])
        for tick, call in zip((0, 1, 3, 7), self.jobs.update_one.call_args_list, strict=True):
            stamp = self.stamp + timedelta(seconds=tick)
            self.assertEqual(call.args, ({**self.fence, "lease_end": {"$gt": stamp}},
                                         {"$set": {"stage": "Voicing", "lease_end": stamp + worker.LEASE}}))
        self.assertEqual(lease.deadline, 7 + worker.LEASE.total_seconds() - worker.LEASE_MARGIN_SECONDS)

    def test_lost_fence_is_sticky_even_if_database_recovers(self):
        lease = self.lease()
        self.jobs.update_one.side_effect = [OperationalError(), Mock(matched_count=0)]
        self.assertFalse(lease.renew({"stage": "Saving"}))
        self.assertFalse(lease.renew({"stage": "Voicing"}))
        self.assertFalse(lease.active())
        self.assertEqual(self.jobs.update_one.call_count, 2)
        lease.stop.wait.assert_called_once_with(1)

    def test_reply_arriving_after_confirmed_window_cannot_revive_worker(self):
        lease = self.lease(10)

        def late_ack(*args):
            self.tick = 12
            return Mock(matched_count=1)

        self.jobs.update_one.side_effect = late_ack
        self.assertFalse(lease.renew())
        self.assertEqual(lease.deadline, 10 - worker.LEASE_MARGIN_SECONDS)
        self.assertFalse(lease.active())
        lease.stop.wait.assert_not_called()

    def test_long_outage_during_render_yields_without_failing_or_refunding_job(self):
        lease = self.lease(16)
        self.jobs.update_one.side_effect = AutoReconnect()

        def engine(command, *, stdout, **kwargs):
            stdout.write("Downloading video\n")
            stdout.flush()
            return Mock(poll=Mock(return_value=0), returncode=0)

        self.enterContext(patch.object(worker, "db", return_value=Mock(jobs=self.jobs)))
        self.enterContext(patch.object(worker, "RenderLease", return_value=lease))
        self.enterContext(patch.object(worker.threading, "Thread"))  # Drive renewal on the render thread.
        self.enterContext(patch.object(worker.subprocess, "Popen", side_effect=engine))
        worker.process({**self.fence, "lease_end": self.stamp + timedelta(seconds=16),
                        "url": "https://www.instagram.com/reel/TestReel/"})
        self.assertEqual([call.args[0] for call in lease.stop.wait.call_args_list], [1, 2, 4, 4])
        self.assertEqual(self.tick, 16 - worker.LEASE_MARGIN_SECONDS)
        self.assertEqual(self.jobs.update_one.call_count, 4)
        for call in self.jobs.update_one.call_args_list:
            self.assertEqual(set(call.args[1]), {"$set"})
            self.assertEqual(call.args[1]["$set"]["stage"], "Downloading")
            self.assertNotIn("state", call.args[1]["$set"])

    def test_uncertain_final_commit_leaves_artifact_for_reference_check(self):
        def engine(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_bytes(b"fixture")
            return Mock(poll=Mock(return_value=0), returncode=0)

        bucket = Mock()
        self.enterContext(patch.object(worker, "db", return_value=Mock(jobs=self.jobs)))
        self.enterContext(patch.object(worker.threading, "Thread"))
        self.enterContext(patch.object(worker.subprocess, "Popen", side_effect=engine))
        self.enterContext(patch.object(worker.subprocess, "check_output", return_value="12"))
        self.enterContext(patch.object(worker.subprocess, "run", return_value=Mock(returncode=1)))
        self.enterContext(patch.object(worker, "media_db"))
        self.enterContext(patch.object(worker.gridfs, "GridFS", return_value=bucket))
        self.enterContext(patch("server.sources.download_to", return_value=(12, "Reel", "")))
        self.jobs.update_one.side_effect = [Mock(matched_count=1), Mock(matched_count=1), OperationalError()]
        worker.process({**self.fence, "lease_end": self.stamp + worker.LEASE,
                        "url": "https://www.instagram.com/reel/TestReel/"})
        self.assertEqual(self.jobs.update_one.call_count, 3)
        query, update = self.jobs.update_one.call_args.args
        self.assertEqual(query, {**self.fence, "lease_end": {"$gt": self.stamp}})
        self.assertEqual(update["$set"]["state"], "done")
        self.assertEqual(update["$set"]["artifact"], bucket.put.return_value)
        bucket.delete.assert_not_called()
