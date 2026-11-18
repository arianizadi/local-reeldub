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

from .db import db, now, setup_indexes

LEASE = timedelta(seconds=90)
MAX_ATTEMPTS = 3
STAGES = {"Downloading video": "Downloading", "Separating original": "Separating audio",
          "Listening with": "Transcribing", "Identifying speakers": "Identifying voices",
          "Translating the source": "Translating", "Synthesizing English": "Voicing",
          "Mixing soundtrack": "Mixing", "Encoding video": "Encoding"}


def claim():
    jobs = db().jobs
    stamp = now()
    expired = {"state": "running", "lease_end": {"$lt": stamp}}
    jobs.update_many({**expired, "attempt": {"$gte": MAX_ATTEMPTS}}, {
        "$set": {"state": "failed", "stage": "Failed", "finished": stamp, "error": "Worker interrupted repeatedly."},
        "$unset": {"slot": "", "lease": ""}})
    doc = jobs.find_one_and_update(
        {"$or": [{"state": "queued"}, {**expired, "attempt": {"$lt": MAX_ATTEMPTS}}]},
        [{"$set": {"state": "running", "stage": "Starting", "lease": str(uuid4()), "lease_end": stamp + LEASE,
                   "started": {"$ifNull": ["$started", stamp]}, "attempt": {"$add": ["$attempt", 1]}}}],
        sort=[("priority", -1), ("created", 1), ("_id", 1)], return_document=ReturnDocument.AFTER)
    return doc


def process(job):
    fence = {"_id": job["_id"], "lease": job["lease"], "state": "running"}
    jobs = db().jobs
    stop = threading.Event()
    lost = threading.Event()

    def heartbeat():
        while not stop.wait(15):
            try:
                if jobs.update_one(fence, {"$set": {"lease_end": now() + LEASE}}).matched_count:
                    continue
            except Exception:
                pass
            lost.set()
            break

    renew = threading.Thread(target=heartbeat, daemon=True)
    renew.start()
    stage = "Starting"
    try:
        with tempfile.TemporaryDirectory(prefix="reeldub-job-") as temp:
            dest = Path(temp) / "dub.mp4"
            log = Path(temp) / "render.log"
            env = {k: v for k, v in os.environ.items() if k not in
                   ("MONGO_URI", "SELF_HOST_KEY", "MONGO_PASSWORD", "ADMIN_APPLE_SUB")}
            env.update(PYTHONUNBUFFERED="1", REELDUB_SERVICE_LIMITS="1", HF_HUB_OFFLINE="1")
            engine = os.getenv("REELDUB_ENGINE", str(Path(__file__).resolve().parents[1] / "dub"))
            with log.open("w") as writer, log.open() as reader:
                proc = subprocess.Popen([engine, job["url"], "-o", dest], stdout=writer,
                                        stderr=subprocess.STDOUT, start_new_session=True, env=env)
                deadline = time.monotonic() + int(os.getenv("JOB_TIMEOUT", "1800"))
                try:
                    while proc.poll() is None:
                        if time.monotonic() >= deadline or lost.is_set():
                            raise RuntimeError("Render timed out or worker lease expired.")
                        for line in reader:
                            update = next((v for k, v in STAGES.items() if line.startswith(k)), None)
                            if update and update != stage:
                                stage = update
                                jobs.update_one(fence, {"$set": {"stage": stage}})
                        time.sleep(0.35)
                finally:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
            if proc.returncode or not dest.is_file():
                raise RuntimeError(f"The render failed during {stage.lower()}. The source may be unavailable.")
            if dest.stat().st_size > 500 * 1024 * 1024:
                raise RuntimeError("Rendered video exceeds the 500 MB service limit.")
            seconds = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", dest], text=True).strip())
            bucket = gridfs.GridFS(db())
            with dest.open("rb") as video:
                artifact = bucket.put(video, filename=job["_id"] + ".mp4", lease=job["lease"], job=job["_id"])
            saved = jobs.update_one(fence, {"$set": {"state": "done", "stage": "Ready", "finished": now(),
                                    "artifact": artifact, "media_seconds": seconds}, "$unset": {"lease": "", "lease_end": ""}})
            if not saved.modified_count:
                bucket.delete(artifact)
    except Exception as exc:
        print(f"Job {job['_id']} failed at {stage} ({type(exc).__name__})", flush=True)
        message = str(exc) if type(exc) is RuntimeError else f"Worker failed during {stage.lower()}."
        jobs.update_one(fence, {"$set": {"state": "failed", "stage": "Failed", "error": message, "finished": now()},
                               "$unset": {"slot": "", "lease": "", "lease_end": ""}})
    finally:
        stop.set()
        renew.join()


def serve():
    indexed = False
    while True:
        try:
            if not indexed:
                setup_indexes()
                indexed = True
            job = claim()
            if job:
                process(job)
            else:
                time.sleep(1)
        except PyMongoError as error:
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
