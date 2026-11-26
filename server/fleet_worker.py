"""A Studio worker with access only to its assigned input and active lease."""
import base64
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import shutil
import subprocess
from pathlib import Path
import time
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from psycopg import OperationalError
import requests

from .gpu import lock_dir, pipeline_slot
from .source import MAX_SOURCE_SECONDS, SCREEN_TEXT_VERSION, TOO_LONG
from .worker import LeaseLost, process
from .upload_voice import VOICE_ERRORS, kitten_match_ready


class VideoBody:
    """A bounded-length iterable lets the coordinator stream the poster and video together."""

    def __init__(self, path, poster):
        self.path = path
        self.poster = poster

    def __len__(self):
        return 4 + len(self.poster) + self.path.stat().st_size

    def __iter__(self):
        yield len(self.poster).to_bytes(4, "big") + self.poster
        with self.path.open("rb") as stream:
            while chunk := stream.read(256 * 1024):
                yield chunk


class Coordinator:
    def __init__(self):
        self.url = os.environ["REELDUB_FLEET_URL"].rstrip("/")
        parsed = urlsplit(self.url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("A private HTTPS coordinator URL is required.")
        self.node = os.environ["REELDUB_FLEET_NODE"]
        self.ca = os.environ["REELDUB_FLEET_CA"]
        self.cert = (os.environ["REELDUB_FLEET_CERT"], os.environ["REELDUB_FLEET_KEY"])
        self.token = Path(os.environ["REELDUB_FLEET_TOKEN_FILE"]).read_text().strip()

    @contextmanager
    def request(self, method, path, *, headers=None, timeout=(5, 25), **options):
        # Heartbeats run concurrently with media transfers and each owns its connection.
        with requests.Session() as session:
            session.trust_env = False
            session.verify, session.cert = self.ca, self.cert
            creds = {"Authorization": "Bearer " + self.token, "X-ReelDub-Node": self.node}
            try:
                with session.request(method, self.url + path, headers={**creds, **(headers or {})},
                                     allow_redirects=False, timeout=timeout, **options) as result:
                    if result.status_code == 409:
                        raise LeaseLost()
                    if result.status_code >= 500 or result.status_code in (408, 429):
                        raise OperationalError("Coordinator temporarily unavailable")
                    if result.status_code in (401, 403):
                        raise PermissionError("Coordinator rejected this worker identity")
                    if result.is_redirect:
                        raise ValueError("Unexpected coordinator redirect")
                    yield result
            except requests.HTTPError as exc:
                raise RuntimeError("The coordinator rejected the render data.") from exc
            except requests.RequestException as exc:
                raise OperationalError("Coordinator connection interrupted") from exc

    def claim(self, slot, request_id):
        sent = time.monotonic()
        body = {"request_id": request_id, "upload_voice_protocol": 1, "match_quality_protocol": 1, "screen_text_protocol": SCREEN_TEXT_VERSION,
                "caption_filter_protocol": 1}
        if kitten_match_ready(require_service=True):
            body["kitten_match"] = True
        with self.request("POST", f"/v1/slots/{slot}/claim", json=body) as r:
            if r.status_code == 204:
                return None
            r.raise_for_status()
            job = r.json()
            if "lease_remaining_seconds" in job:
                remaining = max(0, min(90, job["lease_remaining_seconds"]) - (time.monotonic() - sent))
                job["lease_end"] = datetime.now(timezone.utc) + timedelta(seconds=remaining)
            else:
                job["lease_end"] = datetime.fromisoformat(job["lease_end"])
            return job


class AssignedJob:
    def __init__(self, coordinator, job):
        self.coordinator = coordinator
        self.path = "/v1/jobs/" + job["_id"]
        self.headers = {"X-ReelDub-Lease": job["lease"]}

    def transfer(self, operation, lease):
        for delay in (1, 2, 4, 8, 15):
            if not lease.active():
                raise LeaseLost()
            try:
                return operation()
            except OperationalError:
                lease.stop.wait(delay)
        if not lease.active():
            raise LeaseLost()
        return operation()

    def interrupt(self, reason):
        with self.coordinator.request("POST", self.path + "/infrastructure", headers=self.headers,
                                      json={"reason": reason}) as r:
            r.raise_for_status()

    def update_one(self, _fence, update):
        # RenderLease uses the same retry/deadline logic for both local and remote workers.
        fields = {k: v for k, v in update["$set"].items() if k in {"stage", "progress"}}
        if stamp := update["$set"].get("estimated_finish"):
            fields["estimated_finish"] = stamp.isoformat()
        try:
            with self.coordinator.request("POST", self.path + "/heartbeat", headers=self.headers, json=fields) as r:
                r.raise_for_status()
        except LeaseLost:
            return SimpleNamespace(matched_count=0)
        return SimpleNamespace(matched_count=1)

    def download(self, path):
        with self.coordinator.request("GET", self.path + "/source", headers=self.headers,
                                      stream=True, timeout=(5, 430)) as r:
            if r.status_code in (410, 413, 422):
                raise RuntimeError("ReelDub could not prepare this source video.")
            r.raise_for_status()
            duration = int(r.headers["X-Duration"])
            runtime = float(r.headers.get("X-Estimated-Runtime", 0))
            limit = 250 * 1024**2
            if MAX_SOURCE_SECONDS and duration > MAX_SOURCE_SECONDS:
                raise RuntimeError(TOO_LONG)
            if duration <= 0:
                raise ValueError("Invalid source duration")
            with path.open("wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    if f.tell() + len(chunk) > limit:
                        raise ValueError("Source exceeds service limit")
                    f.write(chunk)
                if f.tell() == 0:
                    raise ValueError("Empty source")
            return duration, runtime if 0 < runtime <= 3600 else None

    def publish(self, path, metadata, poster):
        limits = {"source_language": 24, "first_caption": 240, "asr_model": 120,
                  "translation_model": 120, "voice_model": 120, "screen_text_engine": 120}
        metadata = {**{k: str(metadata[k])[:limit] for k, limit in limits.items() if metadata.get(k)},
                    **{k: metadata[k] for k in ("media_seconds", "audio_mix_version", "rendered_voice_mode",
                                               "screen_text_version", "screen_text_count", "caption_filter_version") if k in metadata}}
        details = base64.urlsafe_b64encode(json.dumps(metadata, ensure_ascii=True, separators=(",", ":")).encode()).decode()
        with self.coordinator.request("PUT", self.path + "/result", headers={
                **self.headers, "Content-Type": "application/octet-stream", "X-ReelDub-Details": details},
                data=VideoBody(path, poster), timeout=(5, 120)) as r:
            r.raise_for_status()

    def fail(self, stage, message):
        reason = "generic"
        if stage == "Downloading":
            reason = "download"
        if "detect speech" in message:
            reason = "no_speech"
        if "produce an English translation" in message:
            reason = "translation"
        if message == TOO_LONG:
            reason = "duration"
        for key, explanation in VOICE_ERRORS.items():
            if message == explanation:
                reason = key
        with self.coordinator.request("POST", self.path + "/failure", headers=self.headers,
                                      json={"stage": stage, "reason": reason}) as r:
            r.raise_for_status()


def preflight():
    root = Path(__file__).resolve().parents[1]
    cache = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))
    hub = Path(os.getenv("HF_HUB_CACHE", Path(os.getenv("HF_HOME", cache / "huggingface")) / "hub"))
    files = [root / ".qwen-venv/ready", root / ".venv/bin/python", root / ".qwen-venv/bin/python",
             cache / "local-reeldub/segmentation.onnx", cache / "local-reeldub/wespeaker_en_voxceleb_resnet34.onnx",
             cache / "bandit-infer/checkpoint-multi.ckpt",
             cache / "reeldub/index-translate/translation.gguf", cache / "reeldub/index-translate/llama-b12532/llama-server"]
    if os.environ.get("REELDUB_ENGINE") == str(root / "deploy/fleet/isolated-engine.sh"):
        files.append(cache / "reeldub/index-translate/llama-b12532/llama-completion")
    weights = (("models--Qwen--Qwen3-TTS-12Hz-1.7B-Base", "*.safetensors"),
               ("models--Systran--faster-whisper-large-v3", "model.bin"))
    if not all(p.is_file() for p in files) or not all(
        any(path.is_file() for path in (hub / name / "snapshots").glob("*/" + pattern)) for name, pattern in weights
    ):
        return "model_unavailable"
    if shutil.disk_usage(os.getenv("TMPDIR", "/tmp")).free < 2 * 1024**3:
        return "runtime_unavailable"
    check = [root / ".qwen-venv/bin/python", "-c", "import torch; assert torch.ones(1,device='cuda').item() == 1"]
    if os.environ.get("REELDUB_ENGINE") == str(root / "deploy/fleet/isolated-engine.sh"):
        check = [root / "deploy/fleet/isolate.sh", "preflight"]
    try:
        subprocess.run(check, timeout=25, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except (OSError, subprocess.SubprocessError):
        return "gpu_unavailable"
    return None


@contextmanager
def preflight_window(*, idle_only):
    """Reserve GPU stages without ever making the health reporter wait on a render."""
    if os.getenv("REELDUB_GPU_SCHEDULER") != "1":
        yield True
        return
    import fcntl
    names = ["translation", "voice"]
    if idle_only:
        slots = int(os.getenv("REELDUB_GPU_PIPELINE_SLOTS", "2"))
        names += [f"pipeline-{slot}" for slot in range(slots)]
    with ExitStack() as held:
        for name in names:
            file = held.enter_context((lock_dir() / name).open("a"))
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                break
        else:
            yield True
            return
    yield False


def report_health(coordinator):
    from .gpu import gpu_snapshot
    needs_check = True
    last_check = last_metrics = float("-inf")
    reason = None
    while True:
        try:
            tick = time.monotonic()
            checked = False
            if (needs_check and tick - last_check >= 30) or tick - last_check >= 900:
                # Routine probes wait for all jobs to finish. Recovery probes can run between
                # scheduled GPU stages, allowing an existing lease to survive a health-store restart.
                with preflight_window(idle_only=not needs_check) as available:
                    if available:
                        checked = True
                        reason = preflight()
                        last_check = tick
            status = {"ready": reason is None, "preflight": checked}
            if kitten_match_ready(require_service=True):
                status["kitten_match"] = True
            if reason:
                status["reason"] = reason
            with coordinator.request("POST", "/v1/slots/0/health", json=status) as r:
                r.raise_for_status()
                needs_check = bool(r.json().get("needs_preflight"))
            if tick - last_metrics >= 60 and os.getenv("REELDUB_LOCAL_GPU_METRICS") == "1":
                last_metrics = tick
                sample = gpu_snapshot()
                if sample:
                    sample.pop("sampled", None)
                    with coordinator.request("POST", "/v1/slots/0/telemetry", json=sample) as r:
                        r.raise_for_status()
                else:
                    needs_check = True
        except (OperationalError, PermissionError, OSError, RuntimeError, ValueError, LeaseLost) as error:
            print(f"Worker health check unavailable ({type(error).__name__})", flush=True)
        time.sleep(15)


def serve():
    coordinator = Coordinator()
    slot = int(os.environ["REELDUB_FLEET_SLOT"])
    if slot == 0:
        threading.Thread(target=report_health, args=(coordinator,), daemon=True).start()
    request_id = str(uuid4())
    while True:
        try:
            with pipeline_slot() as available:
                if available:
                    job = coordinator.claim(slot, request_id)
                    request_id = str(uuid4())
                    if job:
                        print(f"Rendering {job['_id']} in slot {slot}", flush=True)
                        process(job, AssignedJob(coordinator, job))
                        continue
            time.sleep(4 if available else 1)
        except LeaseLost:
            request_id = str(uuid4())
            time.sleep(4)
        except (OperationalError, PermissionError, RuntimeError) as exc:
            print(f"Coordinator unavailable ({type(exc).__name__})", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    serve()
