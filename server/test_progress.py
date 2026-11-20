from datetime import datetime, timedelta, timezone
import unittest

from .progress import line_progress, nearby_runtime


class ProgressTests(unittest.TestCase):
    def test_matching_history_and_sparse_samples(self):
        start = datetime.now(timezone.utc)
        def job(runtime, language="de", length=20):
            return dict(started=start, finished=start + timedelta(seconds=runtime),
                        media_seconds=length, target_language=language)
        self.assertIsNone(nearby_runtime([job(32), job(30)], 20, "de"))
        rows = [job(40), job(80), job(50), job(2), job(10, "en"), job(140, length=360)]
        self.assertEqual(nearby_runtime(rows, 20, "de"), 50)
        self.assertIsNone(nearby_runtime(rows, 0, "de"))

    def test_line_counts_are_bounded_before_publication(self):
        self.assertIsNone(line_progress({"completed": 6, "total": 5}))
        self.assertIsNone(line_progress({"completed": True, "total": 1}))
        self.assertLess(line_progress({"completed": 1, "total": 2}), line_progress({"completed": 2, "total": 2}))
        self.assertLess(line_progress({"completed": 2, "total": 2}), 1.0)
