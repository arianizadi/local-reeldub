import json
from math import ceil
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
from pymongo.errors import ConnectionFailure, ExecutionTimeout, PyMongoError, WTimeoutError

from .source import MAX_SOURCE_SECONDS, TOO_LONG, reel_url
from .languages import outputs
from .upload_voice import KITTEN_ENGINE, KITTEN_MODEL, VOICE_ERRORS, kitten_ready, upload_mode, voice_scope
from .db import media_db, QUEUE_ORDER, db, now, uses_postgres
from .gpu import pipeline_slot
from .wakeup import Wake
from .progress import PREFIX, STEPS, line_progress, nearby_runtime
from psycopg import Error as PostgresError, OperationalError
from psycopg_pool import PoolTimeout

LEASE = timedelta(seconds=90)
HEARTBEAT_SECONDS = 15
LEASE_MARGIN_SECONDS = 5
TEMPORARY_DB_ERRORS = (ConnectionFailure, ExecutionTimeout, WTimeoutError, OperationalError, PoolTimeout)
MAX_ATTEMPTS = 3
STAGES = {"Waiting for GPU capacity": "Waiting for GPU", "GPU stage: ": "Processing", "Downloading video": "Downloading", "Separating original": "Separating audio",
          "Cleaning residual": "Cleaning background voices", "Listening with": "Transcribing", "Identifying speakers": "Identifying voices",
          "Translating the source": "Translating", "Synthesizing ": "Voicing",
          "Mixing soundtrack": "Mixing", "Encoding video": "Encoding"}

def render_error(stage, diagnostic):
    for message in VOICE_ERRORS.values():
        if message.lower() in diagnostic:
            return message
    if TOO_LONG.lower() in diagnostic:
        return TOO_LONG
    if stage == "Downloading":
        if "can't be seen by certain audiences" in diagnostic:
            return "Instagram restricts who can view this Reel. ReelDub could not download it."
        return "ReelDub could not download this video. Check that the link opens publicly."
    if "no translatable speech detected" in diagnostic or "no spoken lines to render" in diagnostic:
        return "ReelDub could not detect speech to dub in this video."
    if "neither translator produced an english dub" in diagnostic:
        return "ReelDub could not produce an English translation for the detected speech."
    return f"The render failed during {stage.lower()}. You can retry or report this Reel."


def claim(studio=None, fleet_slot=None, fleet_request=None, avoid_recovery_node=None, upload_voices=True,
          kitten_narrator=None, alternatives=None):
    database = db()
    stamp = now()
    expired = {"state": "running", "fleet_slot": None, "lease_end": {"$lt": stamp}}
    exhausted = {**expired, "attempt": {"$gte": MAX_ATTEMPTS}}
    failure = {"state": "failed", "stage": "Failed", "finished": stamp, "error": "Worker interrupted repeatedly."}
    release = ["slot", "minute_grants", "minute_units", "render_key", "extra_ticket", "skip_ticket", "lease", "lease_end"]
    if studio is None:
        studio = (Path(__file__).resolve().parents[1] / ".qwen-venv/ready").is_file()
    eligible = outputs("studio" if studio else "light") + [None]
    # A Light worker cannot fulfill a job that promises a Studio quality preset.
    profile_filter = {} if studio else {"render_quality": None, "lab_mode": None}
    candidate = {**profile_filter, "deleted": {"$ne": True}, "target_language": {"$in": eligible}}
    if kitten_narrator is None:
        kitten_narrator = kitten_ready()
    if not (studio and upload_voices and kitten_narrator):
        candidate["narrator_engine"] = None
    if not upload_voices:
        # Earlier workers don't understand upload voices, including uploaded jobs
        # that predate the voice_mode field. They may still render ordinary Reels.
        candidate.update(voice_mode=None, url={"$regex": "^https://"})
    elif not studio:
        candidate["voice_mode"] = {"$ne": "match"}
    if avoid_recovery_node:
        eligible_recovery = [{"recovery_rank": {"$ne": 1}}, {"recovery_from_node": {"$ne": avoid_recovery_node}}]
        if alternatives is not None and KITTEN_ENGINE not in alternatives:
            eligible_recovery.append({"narrator_engine": KITTEN_ENGINE})
        candidate["$and"] = [{"$or": eligible_recovery}]
    start = {"state": "running", "stage": "Starting", "progress": 0.01, "estimated_finish": None,
             "lease": str(uuid4()), "lease_end": stamp + LEASE}
    start.update(fleet_slot=fleet_slot, fleet_request=fleet_request, attempt_started=stamp)
    if fleet_slot:
        start["fleet_upload_voice_protocol"] = 1 if upload_voices else None
    retryable = {**expired, "deleted": {"$ne": True}, "attempt": {"$lt": MAX_ATTEMPTS}}
    if uses_postgres():
        candidate["$or"] = [{"state": "queued"}, retryable]
        return database.claim_job(candidate, exhausted, failure, release, start, stamp)
    jobs = database.jobs
    jobs.update_many(exhausted, {"$set": failure, "$unset": dict.fromkeys(release, "")})
    # Expired direct-worker jobs join the recovery queue first. Fleet jobs are
    # separately fenced and requeued by the node watchdog.
    jobs.update_many(retryable, {"$set": {"state": "queued", "stage": "Recovering render", "recovery_rank": 1},
                                 "$unset": {"lease": "", "lease_end": ""}})
    candidate["state"] = "queued"
    return jobs.find_one_and_update(
        candidate, [{"$set": {**start, "started": {"$ifNull": ["$started", stamp]}, "attempt": {"$add": [{"$ifNull": ["$attempt", 0]}, 1]}}}],
        sort=QUEUE_ORDER, return_document=ReturnDocument.AFTER)


def quality_options(job):
    if upload_mode(job) is not None:
        options = ["--quality", job["render_quality"]] if job.get("render_quality") else []
        scope = voice_scope(job)
        options += ["--upload-voice", scope]
        if engine := job.get("narrator_engine"):
            if engine != KITTEN_ENGINE or scope != "narrator" or job.get("target_language", "en") != "en":
                raise ValueError("Unsupported upload narrator.")
            options += ["--narrator-engine", engine]
        return options
    if mode := job.get("lab_mode"):
        options = ["--quality", mode, "--voice-batch", str(job.get("voice_batch", 2)),
                   "--lab-runtime", job.get("lab_runtime", "standard"),
                   "--lab-separation", job.get("lab_separation", "fp32")]
        if job.get("lab_voice", "default") != "default":
            options += ["--lab-voice", job["lab_voice"]]
        return options
    if quality := job.get("render_quality"):
        return ["--quality", quality]
    return []


class InfrastructureFailure(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class LeaseLost(Exception):
    pass


class RenderLease:
    def __init__(self, jobs, fence, lease_end):
        self.jobs = jobs
        self.fence = fence
        self.lock = threading.Lock()
        self.stop = threading.Event()
        expires = time.monotonic() + (lease_end - now()).total_seconds()
        self.last_renewed = expires - LEASE.total_seconds()
        self.deadline = expires - LEASE_MARGIN_SECONDS

    def active(self):
        return not self.stop.is_set() and time.monotonic() < self.deadline

    def current_fence(self, stamp):
        return {**self.fence, "lease_end": {"$gt": stamp}}

    def renew(self, fields=None):
        with self.lock:
            delay = 1
            while self.active():
                tick = time.monotonic()
                if fields is None and tick - self.last_renewed < HEARTBEAT_SECONDS:
                    return True
                stamp = now()
                update = {"lease_end": stamp + LEASE, **(fields or {})}
                try:
                    result = self.jobs.update_one(self.current_fence(stamp), {"$set": update})
                except TEMPORARY_DB_ERRORS:
                    self.stop.wait(max(0, min(delay, self.deadline - time.monotonic())))
                    delay = min(5, delay * 2)
                    continue
                if not result.matched_count or not self.active():
                    break
                # Time spent waiting for the acknowledgement doesn't extend the lease.
                self.last_renewed = tick
                self.deadline = tick + LEASE.total_seconds() - LEASE_MARGIN_SECONDS
                return True
            self.stop.set()
            return False


def process(job, fleet=None):
    fence = {"_id": job["_id"], "lease": job["lease"], "state": "running"}
    jobs = None if fleet else db().jobs
    lease = RenderLease(fleet or jobs, fence, job["lease_end"])
    if not lease.active():
        return

    def heartbeat():
        try:
            while lease.renew():
                if lease.stop.wait(HEARTBEAT_SECONDS):
                    return
        except Exception as exc:
            print(f"Lease heartbeat interrupted ({type(exc).__name__})", flush=True)
        lease.stop.set()

    renew = threading.Thread(target=heartbeat, daemon=True)
    renew.start()
    stage = "Starting"
    progress = STEPS[stage]
    published = progress
    last_write = 0
    runtime = None
    sampled_duration = None
    try:
        options = quality_options(job)
        if not fleet and (job.get("url") or "").startswith("https://") and os.getenv("REELDUB_REEL_LINKS") == "0":
            raise RuntimeError("Reel links are disabled on this server. Share a video file instead.")
        with tempfile.TemporaryDirectory(prefix="reeldub-job-") as temp:
            dest = Path(temp) / "dub.mp4"
            log = Path(temp) / "render.log"
            names = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "CUDA_VISIBLE_DEVICES",
                     "LD_LIBRARY_PATH", "HF_HOME", "XDG_CACHE_HOME", "REELDUB_GPU_LOCK_DIR", "REELDUB_LANDRUN",
                     "OMP_NUM_THREADS", "MKL_NUM_THREADS", "REELDUB_TTS_BATCH_SIZE"}
            env = {k: v for k, v in os.environ.items() if k in names}
            if not job.get("priority") and os.getenv("REELDUB_FREE_CPU_OVERFLOW") == "1":
                env["REELDUB_FREE_CPU_ASR"] = "1"
            metadata = Path(temp) / "metadata.json"
            env["REELDUB_METADATA_FILE"] = str(metadata)
            env.update(PYTHONUNBUFFERED="1", REELDUB_SERVICE_LIMITS="1", HF_HUB_OFFLINE="1")
            engine = os.getenv("REELDUB_ENGINE", str(Path(__file__).resolve().parents[1] / "dub"))
            if fleet:
                source = Path(temp) / "source.mp4"
                if not lease.renew({"stage": "Downloading", "progress": STEPS["Downloading"]}):
                    raise LeaseLost()
                stage = "Downloading"
                duration, runtime = fleet.transfer(lambda: fleet.download(source), lease)
                env["REELDUB_STAGED_INPUT"] = str(source)
                if duration:
                    env["REELDUB_MAX_SECONDS"] = str(duration)
                input_arg = str(source)
            elif source_file := job.get("source_file"):
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
                command = [engine, input_arg, "--target-language", job.get("target_language", "en"), "-o", dest]
                command += options
                proc = subprocess.Popen(command, stdout=writer,
                                        stderr=subprocess.STDOUT, start_new_session=True, env=env)
                deadline = time.monotonic() + int(os.getenv("JOB_TIMEOUT", "1800"))
                try:
                    while True:
                        finished = proc.poll() is not None
                        if not lease.active():
                            raise LeaseLost()
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Render timed out.")
                        for line in reader:
                            if line.startswith("Index translation unavailable ("):
                                print(f"Job {job['_id']}: {line.strip()[:110]}", flush=True)
                            update = next((v for k, v in STAGES.items() if line.startswith(k)), None)
                            if line.startswith("GPU stage: "):
                                candidate = line.partition(": ")[2].strip()
                                update = candidate if candidate in STEPS else "Processing"
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
                                if not fleet and type(seconds) in (int, float) and 0 < seconds <= MAX_SOURCE_SECONDS and seconds != sampled_duration:
                                    sampled_duration = seconds
                                    try:
                                        rows = jobs.find({"state": "done", "lab_mode": job.get("lab_mode"),
                                                          "narrator_engine": job.get("narrator_engine"),
                                                          "render_quality": job.get("render_quality"), "media_seconds": {"$exists": True}},
                                                         {"media_seconds": 1, "started": 1, "finished": 1,
                                                          "attempt": 1, "target_language": 1}).sort("finished", -1).limit(40)
                                        runtime = nearby_runtime(rows, seconds, job.get("target_language", "en"))
                                    except TEMPORARY_DB_ERRORS:
                                        runtime = None
                                if stage == "Voicing":
                                    progress = max(progress, line_progress(event) or 0)
                            tick = time.monotonic()
                            if previous != stage or progress > published and tick-last_write >= 5:
                                fields = {"stage": stage, "progress": round(progress, 4)}
                                if runtime:
                                    fields["estimated_finish"] = now() + timedelta(seconds=round(runtime*(1-progress)))
                                if not lease.renew(fields):
                                    raise LeaseLost()
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
                if TOO_LONG.lower() in diagnostic:
                    raise RuntimeError(TOO_LONG)
                for message in VOICE_ERRORS.values():
                    if message.lower() in diagnostic:
                        raise RuntimeError(message)
                if fleet:
                    if any(x in diagnostic for x in ("cuda out of memory", "cuda error", "cuda was explicitly requested", "driver version is insufficient")):
                        raise InfrastructureFailure("gpu_unavailable")
                    if any(x in diagnostic for x in ("no such file or directory", "localentrynotfounderror", "missing model")):
                        raise InfrastructureFailure("model_unavailable")
                    if not any(x in diagnostic for x in ("no translatable speech", "no spoken lines", "neither translator produced",
                                                         "invalid data found", "does not contain", "exceeds the", "maximum allowed")):
                        raise InfrastructureFailure("runtime_unavailable")
                raise RuntimeError(render_error(stage, diagnostic))
            if dest.stat().st_size > 500 * 1024 * 1024:
                raise RuntimeError("Rendered video exceeds the 500 MB service limit.")
            seconds = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", dest], text=True, timeout=30).strip())
            details = json.loads(metadata.read_text()) if metadata.is_file() else {}
            expected_voice = upload_mode(job)
            if expected_voice is not None and details.get("rendered_voice_mode") != expected_voice:
                raise RuntimeError("The render engine did not confirm the selected voice mode.")
            if (job.get("narrator_engine") == KITTEN_ENGINE) != (details.get("voice_model") == KITTEN_MODEL):
                raise RuntimeError("The render engine did not confirm the selected narrator.")
            details = {k: v for k, v in details.items() if k in
                       {"title", "author", "first_caption", "source_language", "asr_model",
                        "translation_model", "voice_model", "rendered_voice_mode", "audio_mix_version"}}
            if sampled_duration is not None:
                details["source_seconds"] = ceil(sampled_duration)
            preview = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", str(min(0.65, seconds / 3)),
                                      "-i", dest, "-frames:v", "1", "-vf", "scale=320:-2",
                                      "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"], capture_output=True, timeout=30)
            if preview.returncode == 0 and 0 < len(preview.stdout) < 512_000:
                details["has_poster"] = True
            if not lease.renew({"stage": "Saving", "progress": STEPS["Saving"]}):
                raise LeaseLost()
            if fleet:
                fleet.transfer(lambda: fleet.publish(dest, {**details, "media_seconds": seconds},
                                                     preview.stdout if details.get("has_poster") else b""), lease)
                return
            bucket = gridfs.GridFS(media_db())
            with dest.open("rb") as video:
                artifact = bucket.put(video, **({"poster":preview.stdout} if details.get("has_poster") else {}), filename=job["_id"] + ".mp4", lease=job["lease"], job=job["_id"])
            if not lease.active():
                bucket.delete(artifact)
                raise LeaseLost()
            saved = jobs.update_one(lease.current_fence(now()), {"$set": {"state": "done", "stage": "Ready", "progress": 1.0, "estimated_finish": None, "finished": now(),
                                    "artifact": artifact, "media_seconds": seconds, "awaiting_delivery": True,
                                    "media_expires": now() + timedelta(days=7), **details}, "$unset": {"lease": "", "lease_end": ""}})
            if not saved.modified_count:
                bucket.delete(artifact)
    except InfrastructureFailure as exc:
        if fleet and lease.active():
            fleet.interrupt(exc.reason)
    except (LeaseLost, *TEMPORARY_DB_ERRORS) as exc:
        # Leave the lease and its reservations for the scheduler to recover. An unacknowledged
        # publish may already have committed; the existing orphan sweep reconciles its artifact.
        print(f"Job {job['_id']} yielded at {stage} ({type(exc).__name__})", flush=True)
    except Exception as exc:
        if not lease.active():
            return
        print(f"Job {job['_id']} failed at {stage} ({type(exc).__name__})", flush=True)
        message = str(exc) if type(exc) is RuntimeError else f"Worker failed during {stage.lower()}."
        if fleet:
            if isinstance(exc, (OSError, subprocess.TimeoutExpired)):
                fleet.interrupt("runtime_unavailable")
                return
            fleet.fail(stage, message)
            return
        jobs.update_one(lease.current_fence(now()), {"$set": {"state": "failed", "stage": "Failed", "error": message, "finished": now()},
                               "$unset": {"slot": "", "minute_grants": "", "minute_units": "", "render_key": "", "extra_ticket": "", "skip_ticket": "", "lease": "", "lease_end": ""}})
    finally:
        lease.stop.set()
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
