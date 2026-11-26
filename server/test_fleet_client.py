from contextlib import nullcontext
import fcntl
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

from psycopg import OperationalError

from .fleet_worker import AssignedJob, VideoBody, preflight_window, report_health
from .worker import LeaseLost


class FleetTransferTests(TestCase):
    def test_health_reports_gpu_metrics_on_configured_workers(self):
        client = MagicMock()
        self.enterContext(patch.dict(os.environ, {"REELDUB_LOCAL_GPU_METRICS": "1"}))
        self.enterContext(patch("server.fleet_worker.preflight_window", return_value=nullcontext(True)))
        self.enterContext(patch("server.fleet_worker.preflight", return_value=None))
        self.enterContext(patch("server.gpu.subprocess.run", return_value=Mock(stdout="Test GPU, 20, 256, 4096, 50, 35.0")))
        self.enterContext(patch("server.fleet_worker.time.sleep", side_effect=StopIteration))
        with self.assertRaises(StopIteration):
            report_health(client)
        health, telemetry = client.request.call_args_list
        self.assertEqual(health.args[1], "/v1/slots/0/health")
        self.assertEqual(telemetry.args[1], "/v1/slots/0/telemetry")
        self.assertEqual(telemetry.kwargs["json"]["used_mib"], 256)

    def test_retry_same_render_and_stop_if_lease_was_fenced(self):
        job = AssignedJob(Mock(), {"_id":"one", "lease":"original"})
        lease = Mock()
        lease.active.return_value = True
        operation = Mock(side_effect=[OperationalError(), "published"])
        self.assertEqual(job.transfer(operation, lease), "published")
        self.assertEqual(operation.call_count, 2)
        lease.active.side_effect = [True, False]
        operation.side_effect = OperationalError()
        with self.assertRaises(LeaseLost):
            job.transfer(operation, lease)

    def test_output_retry_sends_identical_bytes(self):
        with TemporaryDirectory() as d:
            file = Path(d) / "result.mp4"
            file.write_bytes(b"sample-video")
            body = VideoBody(file,b"jpeg")
            self.assertEqual(len(body), len(b"sample-video") + 8)
            self.assertEqual(b"".join(body), b"".join(body))

    def test_gpu_preflight_defers_periodic_probe_and_recovery_respects_active_gpu_stages(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {
            "REELDUB_GPU_SCHEDULER": "1", "REELDUB_GPU_LOCK_DIR": folder, "REELDUB_GPU_PIPELINE_SLOTS": "2"
        }):
            with (Path(folder) / "pipeline-1").open("a") as busy:
                fcntl.flock(busy, fcntl.LOCK_EX)
                with preflight_window(idle_only=True) as available:
                    self.assertFalse(available)
                with preflight_window(idle_only=False) as available:
                    self.assertTrue(available)
                with (Path(folder) / "voice").open("a") as voice:
                    fcntl.flock(voice, fcntl.LOCK_EX)
                    with preflight_window(idle_only=False) as available:
                        self.assertFalse(available)
            with preflight_window(idle_only=True) as available:
                self.assertTrue(available)
