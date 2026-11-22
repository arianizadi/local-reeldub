"""Kernel lock regressions for Linux workers. Runs without a GPU or database."""
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from server.gpu import pipeline_slot, stage


@unittest.skipUnless(sys.platform == "linux", "worker scheduling uses Linux flock")
class GPUSchedulerTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.gpu0 = Path(directory) / "gpu0"
        self.gpu1 = Path(directory) / "gpu1"
        self.enterContext(patch.dict(os.environ, {
            "REELDUB_GPU_SCHEDULER": "1",
            "REELDUB_GPU_LOCK_DIR": str(self.gpu0),
        }))

    def test_slots_are_per_device_and_released(self):
        for configured, slots in ((None, 2), ("1", 1), ("2", 2)):
            with self.subTest(configured=configured):
                if configured is None:
                    os.environ.pop("REELDUB_GPU_PIPELINE_SLOTS", None)
                else:
                    os.environ["REELDUB_GPU_PIPELINE_SLOTS"] = configured
                with ExitStack() as running:
                    for _ in range(slots):
                        self.assertTrue(running.enter_context(pipeline_slot()))
                    self.assertFalse(running.enter_context(pipeline_slot()))
                    with patch.dict(os.environ, REELDUB_GPU_LOCK_DIR=str(self.gpu1)):
                        self.assertTrue(running.enter_context(pipeline_slot()))
                with pipeline_slot() as available:
                    self.assertTrue(available)

    def test_invalid_slot_cap_is_rejected(self):
        for invalid in ("0", "3", "many"):
            with self.subTest(invalid=invalid), patch.dict(os.environ, REELDUB_GPU_PIPELINE_SLOTS=invalid):
                with self.assertRaises(ValueError), pipeline_slot():
                    pass

    def test_stage_locks_allow_only_compatible_overlap(self):
        import fcntl

        def available(lane):
            with (self.gpu0 / lane).open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                return True

        for kind, expected in (("translation", (False, True)),
                               ("voice", (True, False)),
                               ("exclusive", (False, False))):
            with self.subTest(kind=kind):
                with stage(kind):
                    self.assertEqual((available("translation"), available("voice")), expected)
                self.assertTrue(available("translation"))
                self.assertTrue(available("voice"))
