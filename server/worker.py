import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from uuid import uuid4
from datetime import timedelta

import gridfs
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from .source import reel_url
from .languages import outputs
from .db import media_db, QUEUE_ORDER, db, now
from .gpu import pipeline_slot
from .wakeup import Wake
from .progress import PREFIX, STEPS, line_progress, nearby_runtime
from psycopg import Error as PostgresError
from psycopg_pool import PoolTimeout

LEASE = timedelta(seconds=90)
HEARTBEAT_SECONDS = 15
MAX_ATTEMPTS = 3
STAGES = {"Waiting for GPU capacity": "Waiting for GPU", "GPU stage: ": "Processing", "Downloading video": "Downloading", "Separating original": "Separating audio",
          "Cleaning residual": "Cleaning background voices", "Listening with": "Transcribing", "Identifying speakers": "Identifying voices",
          "Translating the source": "Translating", "Synthesizing ": "Voicing",
          "Mixing soundtrack": "Mixing", "Encoding video": "Encoding"}

def render_error(stage, diagnostic):
    if stage == "Downloading":
        if "can't be seen by certain audiences" in diagnostic:
            return "Instagram restricts who can view this Reel. ReelDub could not download it."
        return "ReelDub could not download this video. Check that the link opens publicly."
    if "no translatable speech detected" in diagnostic or "no spoken lines to render" in diagnostic:
        return "ReelDub could not detect speech to dub in this video."
    if "neither translator produced an english dub" in diagnostic:
        return "ReelDub could not produce an English translation for the detected speech."
    return f"The render failed during {stage.lower()}. You can retry or report this Reel."


def claim():
    jobs = db().jobs
    stamp = now()
    expired = {"state": "running", "lease_end": {"$lt": stamp}}
    jobs.update_many({**expired, "attempt": {"$gte": MAX_ATTEMPTS}}, {
        "$set": {"state": "failed", "stage": "Failed", "finished": stamp, "error": "Worker interrupted repeatedly."},
        "$unset": {"slot": "", "minute_grants": "", "minute_units": "", "render_key": "", "extra_ticket": "", "skip_ticket": "", "lease": ""}})
    studio = (Path(__file__).resolve().parents[1] / ".qwen-venv/ready").is_file()
    eligible = outputs("studio" if studio else "light") + [None]
    doc = jobs.find_one_and_update(
        {"deleted": {"$ne": True}, "target_language": {"$in": eligible}, "$or": [{"state": "queued"}, {**expired, "attempt": {"$lt": MAX_ATTEMPTS}}]},
        [{"$set": {"state": "running", "stage": "Starting", "progress": 0.01, "estimated_finish": None, "lease": str(uuid4()), "lease_end": stamp + LEASE,
                   "started": {"$ifNull": ["$started", stamp]}, "attempt": {"$add": ["$attempt", 1]}}}],
        sort=QUEUE_ORDER, return_document=ReturnDocument.AFTER)
    return doc


class RenderLease:
    def __init__(self, jobs, fence):
        self.jobs = jobs
        self.fence = fence
        self.lock = threading.Lock()
        self.last_renewed = time.monotonic()

    def renew(self, fields=None):
        with self.lock:
            tick = time.monotonic()
            if fields is None and tick - self.last_renewed < HEARTBEAT_SECONDS:
                return True
            update = {"lease_end": now() + LEASE, **(fields or {})}
            result = self.jobs.update_one(self.fence, {"$set": update})
            if result.matched_count:
                self.last_renewed = tick
            return bool(result.matched_count)


def process(job):
    fence = {"_id": job["_id"], "lease": job["lease"], "state": "running"}
    jobs = db().jobs
    lease = RenderLease(jobs, fence)
    stop = threading.Event()
    lost = threading.Event()

    def heartbeat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                if lease.renew():
                    continue
            except Exception:
                pass
            lost.set()
            break

    renew = threading.Thread(target=heartbeat, daemon=True)
    renew.start()
    stage = "Starting"
    progress = STEPS[stage]
    published = progress
    last_write = 0
    runtime = None
    sampled_duration = None
    try:
        with tempfile.TemporaryDirectory(prefix="reeldub-job-") as temp:
            dest = Path(temp) / "dub.mp4"
            log = Path(temp) / "render.log"
            names = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "CUDA_VISIBLE_DEVICES",
                     "LD_LIBRARY_PATH", "HF_HOME", "XDG_CACHE_HOME", "REELDUB_GPU_LOCK_DIR",
                     "OMP_NUM_THREADS", "MKL_NUM_THREADS", "REELDUB_TTS_BATCH_SIZE"}
            env = {k: v for k, v in os.environ.items() if k in names}
            if not job.get("priority") and os.getenv("REELDUB_FREE_CPU_OVERFLOW") == "1":
                env["REELDUB_FREE_CPU_ASR"] = "1"
            metadata = Path(temp) / "metadata.json"
            env["REELDUB_METADATA_FILE"] = str(metadata)
            env.update(PYTHONUNBUFFERED="1", REELDUB_SERVICE_LIMITS="1", HF_HUB_OFFLINE="1")
            engine = os.getenv("REELDUB_ENGINE", str(Path(__file__).resolve().parents[1] / "dub"))
            if source_file := job.get("source_file"):
                source = Path(temp)/"source.mov"
                with gridfs.GridFS(media_db()).get(source_file) as stored, source.open("wb") as out:
                    from shutil import copyfileobj
                    copyfileobj(stored,out,256*1024)
                env["REELDUB_STAGED_INPUT"]=str(source)
                env["REELDUB_MAX_SECONDS"]=str(job["quoted_seconds"])
                input_arg=str(source)
            else:
                input_arg=reel_url(job["url"])
            with log.open("w") as writer, log.open() as reader:
                proc = subprocess.Popen([engine, input_arg, "--target-language", job.get("target_language", "en"), "-o", dest], stdout=writer,
                                        stderr=subprocess.STDOUT, start_new_session=True, env=env)
                deadline = time.monotonic() + int(os.getenv("JOB_TIMEOUT", "1800"))
                try:
                    while True:
                        finished = proc.poll() is not None
                        if time.monotonic() >= deadline or lost.is_set():
                            raise RuntimeError("Render timed out or worker lease expired.")
                        for line in reader:
                            if line.startswith("Index translation unavailable ("):
                                print(f"Job {job['_id']}: {line.strip()[:110]}", flush=True)
                            update = next((v for k, v in STAGES.items() if line.startswith(k)), None)
                            if line.startswith("GPU stage: "):
                                update = line.partition(": ")[2].strip()
                            previous = stage
                            if update:
                                stage = update
                                progress = max(progress, STEPS.get(stage, 0))
                            if line.startswith(PREFIX):
                                try:
                                    event = json.loads(line[len(PREFIX):])
                                except ValueError:
                                    continue
                                if not isinstance(event, dict):
                                    continue
                                seconds = event.get("duration")
                                if type(seconds) in (int, float) and 0 < seconds <= 600 and seconds != sampled_duration:
                                    rows = jobs.find({"state": "done", "media_seconds": {"$exists": True}},
                                                     {"media_seconds": 1, "started": 1, "finished": 1,
                                                      "attempt": 1, "target_language": 1}).sort("finished", -1).limit(40)
                                    runtime = nearby_runtime(rows, seconds, job.get("target_language", "en"))
                                    sampled_duration = seconds
                                if stage == "Voicing":
                                    progress = max(progress, line_progress(event) or 0)
                            tick = time.monotonic()
                            if previous != stage or progress > published and tick-last_write >= 5:
                                fields = {"stage": stage, "progress": round(progress, 4)}
                                if runtime:
                                    fields["estimated_finish"] = now() + timedelta(seconds=round(runtime*(1-progress)))
                                if not lease.renew(fields):
                                    raise RuntimeError("Worker lease expired.")
                                published, last_write = progress, tick
                        if finished:
                            break
                        time.sleep(0.35)
                finally:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
            if proc.returncode or not dest.is_file():
                with log.open("rb") as trace:
                    trace.seek(max(0, log.stat().st_size - 16384))
                    diagnostic = trace.read().decode(errors="replace").lower()
                print(f"Render {job['_id']} exited {proc.returncode} at {stage}", flush=True)
                raise RuntimeError(render_error(stage, diagnostic))
            if dest.stat().st_size > 500 * 1024 * 1024:
                raise RuntimeError("Rendered video exceeds the 500 MB service limit.")
            seconds = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", dest], text=True).strip())
            details = json.loads(metadata.read_text()) if metadata.is_file() else {}
            details = {k: v for k, v in details.items() if k in
                       {"title", "author", "first_caption", "source_language", "asr_model",
                        "translation_model", "voice_model", "audio_mix_version"}}
            preview = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", str(min(0.65, seconds / 3)),
                                      "-i", dest, "-frames:v", "1", "-vf", "scale=320:-2",
                                      "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"], capture_output=True, timeout=30)
            if preview.returncode == 0 and 0 < len(preview.stdout) < 512_000:
                details["has_poster"] = True
            if not lease.renew({"stage": "Saving", "progress": STEPS["Saving"]}):
                raise RuntimeError("Worker lease expired.")
            bucket = gridfs.GridFS(media_db())
            with dest.open("rb") as video:
                artifact = bucket.put(video, **({"poster":preview.stdout} if details.get("has_poster") else {}), filename=job["_id"] + ".mp4", lease=job["lease"], job=job["_id"])
            saved = jobs.update_one(fence, {"$set": {"state": "done", "stage": "Ready", "progress": 1.0, "estimated_finish": None, "finished": now(),
                                    "artifact": artifact, "media_seconds": seconds, "awaiting_delivery": True,
                                    "media_expires": now() + timedelta(days=7), **details}, "$unset": {"lease": "", "lease_end": ""}})
            if not saved.modified_count:
                bucket.delete(artifact)
    except Exception as exc:
        print(f"Job {job['_id']} failed at {stage} ({type(exc).__name__})", flush=True)
        message = str(exc) if type(exc) is RuntimeError else f"Worker failed during {stage.lower()}."
        jobs.update_one(fence, {"$set": {"state": "failed", "stage": "Failed", "error": message, "finished": now()},
                               "$unset": {"slot": "", "minute_grants": "", "minute_units": "", "render_key": "", "extra_ticket": "", "skip_ticket": "", "lease": "", "lease_end": ""}})
    finally:
        stop.set()
        renew.join()


def serve():
    wake = Wake()
    draining = True
    indexed = False
    while True:
        try:
            if not indexed:
                db().command("ping")
                indexed = True
            if not draining:
                wake.wait()
                if not wake.due():
                    continue
            with pipeline_slot() as available:
                job = claim() if available else None
                if available:
                    draining = bool(job)
                    if not job:
                        wake.watch_active_leases()
                else:
                    draining = True
                if job:
                    process(job)
            if not available:
                time.sleep(0.5)
        except (PyMongoError, PostgresError, PoolTimeout) as error:
            draining = True
            print(f"Database temporarily unavailable ({type(error).__name__})", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    count = max(1, min(16, int(os.getenv("WORKER_PROCESSES", "1"))))
    children = [multiprocessing.get_context("spawn").Process(target=serve) for _ in range(count)]
    for child in children:
        child.start()
    try:
        for child in children:
            child.join()
    finally:
        for child in children:
            child.terminate()
