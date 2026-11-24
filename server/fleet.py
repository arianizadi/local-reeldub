"""Private GPU coordinator. Serve on loopback behind the node mTLS gateway."""
import asyncio
import base64
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from hashlib import sha256
import logging
import re
from secrets import compare_digest
from tempfile import TemporaryFile
from threading import BoundedSemaphore, Lock
import time
from typing import Literal
from uuid import UUID, uuid4

import gridfs
from bson import ObjectId
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from . import fleet_health, sources, worker
from .upload_voice import upload_mode, voice_scope, VOICE_ERRORS
from .db import db, media_db, now, separate_metadata, uses_postgres
from .progress import STEPS, nearby_runtime
from .source import MAX_SOURCE_SECONDS, TOO_LONG
from .wakeup import Wake, notify


@asynccontextmanager
async def lifespan(app):
    if not separate_metadata():
        raise RuntimeError("The private coordinator requires a configured metadata store.")
    db().command("ping")
    try:
        if not isinstance(fleet_health.registry(), dict):
            raise ValueError
    except (KeyError, ValueError, OSError, TypeError):
        raise RuntimeError("The private worker registry must be available at startup.") from None
    async def monitor():
        while True:
            try:
                await run_in_threadpool(fleet_health.watchdog)
            except Exception as exc:
                logging.getLogger(__name__).warning("Fleet health scan interrupted (%s)", type(exc).__name__)
            await asyncio.sleep(5)
    task = asyncio.create_task(monitor())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
MAX_VIDEO = 500 * 1024 * 1024
MAX_POSTER = 512_000
STAGES = set(STEPS) | {"Waiting for GPU", "Processing"}
CLAIM_FIELDS = ("_id", "lease", "lease_end", "priority", "target_language", "render_quality",
                "lab_mode", "lab_runtime", "lab_separation", "lab_voice", "voice_batch", "quoted_seconds")
claim_capacity = BoundedSemaphore(3)  # Each advisory-lock holder also needs a SQL connection for its claim.


@app.middleware("http")
async def bound_control_messages(request: Request, call_next):
    # Only the streamed binary PUT accepts large bodies; JSON is parsed eagerly by FastAPI.
    if request.method == "POST":
        length = request.headers.get("content-length", "")
        if not length.isascii() or not length.isdigit() or len(length) > 4 or int(length) > 4096:
            return JSONResponse(status_code=413, content={"detail": "Control message exceeds the size limit."})
    return await call_next(request)


@dataclass(frozen=True)
class Node:
    name: str
    slots: int
    studio: bool
    enabled: bool


def authenticated(request: Request):
    name = request.headers.get("x-reeldub-node", "")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or scheme.lower() != "bearer" or not 32 <= len(token) <= 512:
        raise HTTPException(401, "Worker authentication required.")
    try:
        record = fleet_health.registry().get(name, {})
        digest = record.get("token_sha256", "")
        slots = record.get("slots", 0)
        studio = record.get("studio", False)
        enabled = record.get("enabled", True)
        if not (type(slots) is int and 1 <= slots <= 32 and type(studio) is bool
                and type(enabled) is bool and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest)
                and compare_digest(sha256(token.encode()).hexdigest(), digest)):
            raise HTTPException(401, "Worker authentication required.")
    except (ValueError, OSError, KeyError, AttributeError, TypeError):
        raise HTTPException(503, "Worker registry unavailable.")
    return Node(name, slots, studio, enabled)


def temporary_error(request, error):
    return JSONResponse(status_code=503, content={"detail": "Private storage temporarily unavailable."})


for temporary in worker.TEMPORARY_DB_ERRORS:
    app.add_exception_handler(temporary, temporary_error)


class StrictModel(BaseModel):
    model_config = {"extra": "forbid", "allow_inf_nan": False}


class Claim(StrictModel):
    request_id: UUID
    upload_voice_protocol: Literal[1] | None = None


class Heartbeat(StrictModel):
    stage: str | None = Field(default=None, max_length=40)
    progress: float | None = Field(default=None, ge=0, le=1)
    estimated_finish: datetime | None = None


class Failure(StrictModel):
    stage: str = Field(max_length=40)
    reason: Literal["generic", "no_speech", "download", "translation", "duration",
                    "voice_consent", "single_speaker", "unclear_speaker", "upload_translation"]


class Infrastructure(StrictModel):
    reason: Literal["gpu_unavailable", "model_unavailable", "runtime_unavailable"]


class Health(StrictModel):
    ready: bool
    preflight: bool
    reason: Literal["gpu_unavailable", "model_unavailable", "runtime_unavailable"] | None = None


@app.post("/v1/slots/{slot}/health")
def health(slot: int, body: Health, node: Node = Depends(authenticated)):
    if slot != 0:
        raise HTTPException(403, "Only the first slot reports node health.")
    return fleet_health.health(node, body.ready, body.preflight, body.reason)


class Telemetry(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    load: int = Field(ge=0, le=100)
    used_mib: int = Field(ge=0, le=8_388_608)
    total_mib: int = Field(gt=0, le=8_388_608)
    temperature: int = Field(ge=-40, le=150)
    watts: float | None = Field(default=None, ge=0, le=10000)


@app.post("/v1/slots/{slot}/telemetry", status_code=204)
def telemetry(slot: int, body: Telemetry, node: Node = Depends(authenticated)):
    if slot != 0:
        raise HTTPException(403, "Only the first slot samples this node.")
    if not body.name.isprintable() or body.used_mib > body.total_mib:
        raise HTTPException(422, "Invalid GPU sample.")
    stamp = now()
    try:
        media_db().fleet_telemetry.update_one(
            {"_id": node.name, "sampled": {"$lte": stamp - timedelta(minutes=1)}},
            {"$set": {**body.model_dump(), "sampled": stamp}}, upsert=True)
    except DuplicateKeyError:
        pass  # A sample for this node was already accepted within this minute.
    return Response(status_code=204)


class Details(StrictModel):
    media_seconds: float = Field(gt=0, le=MAX_SOURCE_SECONDS + 1)
    source_language: str | None = Field(default=None, max_length=24)
    first_caption: str | None = Field(default=None, max_length=240)
    asr_model: str | None = Field(default=None, max_length=120)
    translation_model: str | None = Field(default=None, max_length=120)
    voice_model: str | None = Field(default=None, max_length=120)
    rendered_voice_mode: Literal["narrator", "match"] | None = None
    audio_mix_version: int | None = Field(default=None, ge=0, le=1000)
    has_poster: bool | None = None


class Slot:
    def __init__(self):
        self.wake = Wake()
        self.draining = True
        self.lock = Lock()


@lru_cache(maxsize=512)
def slot_state(key, upload_voice_protocol=None):
    return Slot()


def claim_reply(job):
    reply = {key: job[key] for key in CLAIM_FIELDS if key in job}
    reply["lease_remaining_seconds"] = max(0, min(worker.LEASE.total_seconds(), (job["lease_end"] - now()).total_seconds()))
    if (mode := upload_mode(job)) is not None:
        reply["voice_mode"] = mode
        if mode == "match":
            # Only the consent scope is needed for single-speaker enforcement.
            reply["voice_consent"] = job.get("voice_consent")
            reply["voice_consent_version"] = job.get("voice_consent_version")
    return reply


@contextmanager
def claim_lock(slot_key):
    busy = HTTPException(503, "Worker slot is assigning a job. Retry shortly.")
    if uses_postgres():
        with db().connection() as conn:
            if not conn.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                                ("reeldub/fleet/" + slot_key,)).fetchone()[0]:
                raise busy
            yield
        return
    # Cross-replica mutex. Unique job indexes independently fence slot and
    # request assignments even if a paused coordinator outlives this lease.
    stamp, token = now(), uuid4().hex
    locks = db().fleet_claim_locks
    try:
        locks.update_one({"_id": slot_key, "expires": {"$lte": stamp}},
                         {"$set": {"expires": stamp + worker.LEASE, "token": token}}, upsert=True)
    except DuplicateKeyError:
        raise busy from None
    try:
        yield
    finally:
        locks.delete_one({"_id": slot_key, "token": token})


@app.post("/v1/slots/{slot}/claim")
def claim(slot: int, body: Claim, node: Node = Depends(authenticated)):
    if not 0 <= slot < node.slots:
        raise HTTPException(403, "Worker slot unavailable.")
    slot_key = f"{node.name}:{slot}"
    req = str(body.request_id)
    key = {"_id": slot_key + "/" + req}
    gate = slot_state(slot_key, body.upload_voice_protocol)
    store = fleet_health.tickets()
    with gate.lock:
        replay = store.find_one(key)
        if replay and replay.get("empty"):
            return Response(status_code=204)
        health_doc = media_db().fleet_nodes.find_one({"_id": node.name})
        if not replay and fleet_health.status(health_doc, now(), enabled=node.enabled) != "online":
            return Response(status_code=204)
        if not replay and not gate.wake.due() and not gate.draining:
            return Response(status_code=204)
        gate.draining = True
        if not claim_capacity.acquire(blocking=False):
            raise HTTPException(503, "Coordinator is assigning work. Retry shortly.")
        try:
            with claim_lock(slot_key):
                # Read again inside the distributed lock. A replica might have assigned this request.
                replay = store.find_one(key)
                if replay and replay.get("empty"):
                    return Response(status_code=204)
                health_doc = media_db().fleet_nodes.find_one({"_id": node.name})
                health_state = fleet_health.status(health_doc, now(), enabled=node.enabled)
                if health_state != "online" and not (replay and health_state == "draining"):
                    return Response(status_code=204)
                lookup = {"fleet_slot": slot_key, "fleet_request": req}
                if replay and replay.get("job"):
                    lookup.update(_id=replay["job"], lease=replay["lease"])
                existing = db().jobs.find_one(lookup)
                if (existing and existing["state"] == "running" and not existing.get("deleted")
                        and existing.get("lease") and existing.get("lease_end", now()) > now()):
                    if not body.upload_voice_protocol and upload_mode(existing) is not None:
                        return Response(status_code=204)
                    return claim_reply(existing)
                if existing or replay:
                    # A pending ticket with no surviving assignment means the previous attempt
                    # stopped before claiming or was already recovered. Never dequeue a second job.
                    return Response(status_code=204)
                ttl = {"expires": now() + timedelta(days=1), "watch": True, "node": node.name,
                       "slot": slot_key, "request": req, "due": now() + worker.LEASE}
                # Persist before claiming: a dropped reply can be recovered by any coordinator.
                store.update_one(key, {"$setOnInsert": ttl}, upsert=True)
                active = db().jobs.find_one({"fleet_slot": slot_key, "state": "running",
                                             "lease_end": {"$gt": now()}}, {"_id": 1})
                avoid = None
                if not active and health_doc and health_doc.get("recoveries", 0) and fleet_health.idle_alternative(node):
                    avoid = node.name
                job = None if active else worker.claim(studio=node.studio, fleet_slot=slot_key, fleet_request=req,
                                                       avoid_recovery_node=avoid, upload_voices=bool(body.upload_voice_protocol))
                if not job:
                    gate.wake.watch_active_leases()
                    if avoid and db().jobs.find_one({"state": "queued", "deleted": {"$ne": True},
                                                     "recovery_rank": 1, "recovery_from_node": avoid}, {"_id": 1}):
                        gate.wake.next_scan = min(gate.wake.next_scan, time.monotonic() + 10)
                    store.update_one(key, {"$set": {"empty": True, "watch": False}})
                    gate.draining = False
                    return Response(status_code=204)
                fleet_health.watch(job)
                return claim_reply(job)
        finally:
            claim_capacity.release()


def requested_fence(job_id: UUID, request: Request, node: Node = Depends(authenticated)):
    lease = request.headers.get("x-reeldub-lease", "")
    if len(lease) != 36:
        raise HTTPException(409, "Worker lease unavailable.")
    slots = [f"{node.name}:{n}" for n in range(node.slots)]
    return {"_id": str(job_id), "lease": lease, "state": "running", "deleted": {"$ne": True},
            "fleet_slot": {"$in": slots}, "lease_end": {"$gt": now()}, **age_limit(now())}


def age_limit(stamp):
    oldest = stamp - fleet_health.MAX_ATTEMPT_AGE
    return {"$or": [{"attempt_started": {"$gt": oldest}},
                    {"attempt_started": None, "started": {"$gt": oldest}}]}


def current_job(scope=Depends(requested_fence)):
    job = db().jobs.find_one(scope)
    if not job:
        raise HTTPException(409, "Worker lease unavailable.")
    if upload_mode(job) is not None:
        if job.get("fleet_upload_voice_protocol") != 1:
            # A node assigned by the previous coordinator may still hold a valid
            # lease. Transfer its reservation to a worker that respects consent.
            fleet_health.recover(job, "upload_voice_upgrade")
            raise HTTPException(409, "Upload reassigned to a compatible worker.")
        try:
            voice_scope(job)
        except RuntimeError as exc:
            failure(Failure(stage="Starting", reason="voice_consent"), fence(job))
            raise HTTPException(409, VOICE_ERRORS["voice_consent"]) from exc
    return job


def fence(job):
    return {"_id": job["_id"], "lease": job["lease"], "fleet_slot": job["fleet_slot"],
            "state": "running", "deleted": {"$ne": True}, "lease_end": {"$gt": now()}, **age_limit(now())}


def changed(result):
    if not result.matched_count:
        raise HTTPException(409, "Worker lease unavailable.")


@app.post("/v1/jobs/{job_id}/heartbeat", status_code=204)
def heartbeat(body: Heartbeat, scope=Depends(requested_fence), node: Node = Depends(authenticated)):
    fields = body.model_dump(exclude_none=True)
    if body.stage is not None and body.stage not in STAGES:
        raise HTTPException(422, "Invalid render stage.")
    if body.estimated_finish is not None and (body.estimated_finish.utcoffset() is None
            or not now() <= body.estimated_finish <= now() + timedelta(hours=2)):
        fields.pop("estimated_finish", None)
    update = {"$set": {**fields, "lease_end": now() + worker.LEASE}}
    if body.progress is not None:
        del update["$set"]["progress"]
        update["$max"] = {"progress": min(0.99, body.progress)}
    stamp = now()
    lease_scope = {**scope, "lease_end": {"$gt": stamp}, **age_limit(stamp + worker.LEASE)}
    nodes = media_db().fleet_nodes
    allowed = {"_id": node.name, "last_seen": {"$gt": stamp - fleet_health.OFFLINE},
               "offline_marked": {"$ne": True}, "reason": None,
               "$or": [{"ready": True}, {"probe_failed_at": {"$ne": None}, "last_preflight": {"$ne": None}}]}

    def health_unavailable():
        state = nodes.find_one({"_id": node.name}, {"reason": 1, "offline_marked": 1}) or {}
        revoked = state.get("offline_marked") or state.get("reason") in fleet_health.REASONS
        raise HTTPException(409 if revoked else 503, "Render node requires a fresh health check.")

    if not nodes.find_one(allowed, {"_id": 1}):
        if not db().jobs.find_one(lease_scope, {"_id": 1}):
            raise HTTPException(409, "Worker lease unavailable.")
        health_unavailable()
    changed(db().jobs.update_one(lease_scope, update))
    # Only a verified, current job may advance the node's liveness timestamp.
    if not nodes.update_one(allowed, {"$max": {"last_seen": stamp}}).matched_count:
        health_unavailable()
    fleet_health.tickets().update_one({"job": scope["_id"], "lease": scope["lease"], "watch": True},
                                      {"$max": {"due": update["$set"]["lease_end"]}})
    return Response(status_code=204)


@app.get("/v1/jobs/{job_id}/source")
def source(job=Depends(current_job)):
    def resolve():
        try:
            return sources.for_job(job)
        except HTTPException as exc:
            if exc.status_code == 422 and exc.detail == TOO_LONG:
                # Finalize on the coordinator so workers on an older release also release the reservation.
                failure(Failure(stage="Downloading", reason="duration"), fence(job))
                raise HTTPException(409, TOO_LONG) from exc
            raise

    if job.get("stage") == "Starting":
        db().jobs.update_one({**fence(job), "stage": "Starting"},
                             {"$set": {"stage": "Downloading"}, "$max": {"progress": STEPS["Downloading"]}})
    doc = resolve()
    bucket = gridfs.GridFS(media_db())
    try:
        stream = bucket.get(doc["_id"])
    except gridfs.NoFile:
        if job.get("source_file"):
            raise HTTPException(410, "The confirmed source is no longer available.")
        # Daily Reels can be staged again when media is missing after a storage recovery.
        db().sources.update_one({"_id": doc["_id"], "owner": job["owner"], "ready": True},
                                {"$set": {"ready": False, "retiring": True, "expires": now()}, "$unset": {"slot": ""}})
        doc = resolve()
        try:
            stream = bucket.get(doc["_id"])
        except gridfs.NoFile:
            raise HTTPException(503, "Private media storage temporarily unavailable.")
    if not 0 < stream.length <= sources.DAILY_REEL_BYTES:
        stream.close()
        raise HTTPException(413, "Source exceeds the transfer limit.")
    duration = min(doc["seconds"], job.get("quoted_seconds") or doc["seconds"])
    headers = {"Content-Length": str(stream.length), "X-Duration": str(duration), "Cache-Control": "no-store"}
    try:
        rows = db().jobs.find({"state": "done", "lab_mode": job.get("lab_mode"), "render_quality": job.get("render_quality"),
                               "media_seconds": {"$exists": True}}, {"media_seconds": 1, "started": 1, "finished": 1,
                               "attempt": 1, "target_language": 1}).sort("finished", -1).limit(40)
        runtime = nearby_runtime(rows, duration, job.get("target_language", "en"))
        if runtime is not None:
            headers["X-Estimated-Runtime"] = str(round(runtime, 2))
    except worker.TEMPORARY_DB_ERRORS:
        pass

    def blocks():
        check = time.monotonic() + 15
        try:
            while part := stream.read(256 * 1024):
                if time.monotonic() >= check:
                    if not db().jobs.find_one(fence(job), {"_id": 1}):
                        return
                    check = time.monotonic() + 15
                yield part
        finally:
            stream.close()
    return StreamingResponse(blocks(), headers=headers, media_type="video/mp4")


@app.post("/v1/jobs/{job_id}/failure", status_code=204)
def failure(body: Failure, scope=Depends(requested_fence)):
    if body.stage not in STAGES:
        raise HTTPException(422, "Invalid render stage.")
    diagnostic = {"generic": "", "download": "", "no_speech": "no translatable speech detected",
                  "translation": "neither translator produced an english dub", "duration": TOO_LONG.lower(),
                  **{key: message.lower() for key, message in VOICE_ERRORS.items()}}[body.reason]
    message = worker.render_error("Downloading" if body.reason == "download" else body.stage, diagnostic)
    release = ("slot", "minute_grants", "minute_units", "render_key", "extra_ticket", "skip_ticket",
               "lease", "lease_end", "upload_lease", "upload_until", "upload_token")
    changed(db().jobs.update_one({**scope, "lease_end": {"$gt": now()}}, {"$set": {"state": "failed", "stage": "Failed", "error": message,
                                                      "finished": now()}, "$unset": dict.fromkeys(release, "")}))
    notify()
    fleet_health.tickets().update_one({"job": scope["_id"], "lease": scope["lease"]}, {"$set": {"watch": False}})
    return Response(status_code=204)


@app.post("/v1/jobs/{job_id}/infrastructure", status_code=204)
def infrastructure(body: Infrastructure, job=Depends(current_job)):
    fleet_health.fault(job["fleet_slot"].rsplit(":", 1)[0], body.reason)
    if not fleet_health.recover(job, body.reason):
        raise HTTPException(409, "Worker lease unavailable.")
    return Response(status_code=204)


def result_details(request):
    value = request.headers.get("x-reeldub-details", "")
    try:
        if not 0 < len(value) <= 8192:
            raise ValueError
        raw = base64.b64decode(value, altchars=b"-_", validate=True)
        return Details.model_validate_json(raw).model_dump(exclude_none=True, exclude={"has_poster"})
    except (ValueError, ValidationError):
        raise HTTPException(422, "Invalid render metadata.")


def publish(job, upload_token, file, size, details):
    file.seek(0)
    poster_len = int.from_bytes(file.read(4), "big")
    video_size = size - 4 - poster_len
    if poster_len > MAX_POSTER or not 12 <= video_size <= MAX_VIDEO:
        raise HTTPException(422, "Invalid media lengths.")
    poster = file.read(poster_len)
    if poster and not (poster.startswith(b"\xff\xd8") and poster.endswith(b"\xff\xd9")):
        raise HTTPException(422, "Invalid poster.")
    if file.read(12)[4:8] != b"ftyp":
        raise HTTPException(422, "Invalid MP4 file.")
    file.seek(4 + poster_len)
    if not db().jobs.find_one({**fence(job), "upload_token": upload_token}, {"_id": 1}):
        raise HTTPException(409, "Worker lease unavailable.")
    bucket = gridfs.GridFS(media_db())
    artifact = ObjectId()
    pending = media_db().fleet_uploads
    pending.insert_one({"_id": artifact, "job": job["_id"], "token": upload_token, "due": now() + timedelta(hours=1)})
    try:
        bucket.put(file, _id=artifact, filename=job["_id"] + ".mp4", lease=job["lease"], job=job["_id"],
                   **({"poster": poster} if poster else {}))
    except Exception:
        bucket.delete(artifact)  # Includes interrupted chunks; the job has not referenced this ID.
        pending.delete_one({"_id": artifact})
        raise
    # If the commit reply is lost, the existing orphan sweeper reconciles by artifact ID.
    result = db().jobs.update_one({**fence(job), "upload_token": upload_token}, {
        "$set": {**details, "state": "done", "stage": "Ready", "progress": 1.0, "estimated_finish": None,
                 "finished": now(), "artifact": artifact, "has_poster": bool(poster),
                 "awaiting_delivery": True, "media_expires": now() + timedelta(days=7)},
        "$unset": dict.fromkeys(("lease", "lease_end", "upload_lease", "upload_until", "upload_token"), "")})
    if not result.matched_count:
        bucket.delete(artifact)
        pending.delete_one({"_id": artifact})
        raise HTTPException(409, "Worker lease unavailable.")
    pending.delete_one({"_id": artifact})
    notify()
    fleet_health.tickets().update_one({"job": job["_id"], "lease": job["lease"]}, {"$set": {"watch": False}})


@app.put("/v1/jobs/{job_id}/result", status_code=204)
async def result(request: Request, job=Depends(current_job)):
    if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
        raise HTTPException(415, "Binary result required.")
    details = result_details(request)
    expected_voice = upload_mode(job)
    if expected_voice is not None and details.get("rendered_voice_mode") != expected_voice:
        raise HTTPException(422, "Rendered voice mode does not match the confirmed job.")
    if not job.get("staged_source") or job.get("staged_lease") != job["lease"]:
        raise HTTPException(422, "Prepare the assigned source first.")
    if job.get("quoted_seconds") and details["media_seconds"] > job["quoted_seconds"] + 1:
        raise HTTPException(422, "Result exceeds the confirmed duration.")
    limit = MAX_VIDEO + MAX_POSTER + 4
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or len(declared) > 12 or not 16 <= int(declared) <= limit):
        raise HTTPException(413, "Result exceeds the transfer limit.")
    token = str(uuid4())
    reserved = await run_in_threadpool(db().jobs.update_one, {
        **fence(job), "$and": [{"$or": [{"upload_lease": {"$ne": job["lease"]}}, {"upload_until": {"$lt": now()}}]}]},
        {"$set": {"upload_lease": job["lease"], "upload_until": now() + timedelta(seconds=60), "upload_token": token}})
    if not reserved.matched_count:
        raise HTTPException(429, "A result upload is already in progress.")
    committed = False
    try:
        with TemporaryFile() as file:
            async with asyncio.timeout(300):
                size = 0
                check = time.monotonic() + 15
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "Result exceeds the transfer limit.")
                    if time.monotonic() >= check:
                        extended = await run_in_threadpool(db().jobs.update_one, {**fence(job), "upload_token": token},
                                                           {"$set": {"upload_until": now() + timedelta(seconds=60)}})
                        if not extended.matched_count:
                            raise HTTPException(409, "Worker lease unavailable.")
                        check = time.monotonic() + 15
                    if chunk:
                        await run_in_threadpool(file.write, chunk)
            await run_in_threadpool(publish, job, token, file, size, details)
            committed = True
    except TimeoutError:
        raise HTTPException(408, "Result upload timed out.")
    finally:
        if not committed:
            await run_in_threadpool(db().jobs.update_one, {**fence(job), "upload_token": token},
                                    {"$unset": {"upload_lease": "", "upload_until": "", "upload_token": ""}})
    return Response(status_code=204)
