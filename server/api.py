import asyncio
import hashlib
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4
from tempfile import TemporaryFile
from urllib.parse import unquote
import time

import gridfs
from psycopg import OperationalError
from psycopg_pool import PoolTimeout
from fastapi.responses import JSONResponse
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from fastapi.concurrency import contextmanager_in_threadpool
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, ExecutionTimeout, WTimeoutError
from starlette.concurrency import run_in_threadpool

from .web import router as web_router
from .wakeup import next_hour, notify
from .db import separate_metadata, uses_postgres, media_db, db, now, setup_indexes
from . import sources
from .video_url import video_url
from .source import CAPTION_FILTER_VERSION, SCREEN_TEXT_VERSION
from .languages import LANGUAGES
from .upload_voice import KITTEN_MATCH

MEDIA_WINDOW = timedelta(hours=24)
def hash_value(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_url(value):
    sources.require_reel_links()
    try:
        return video_url(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def auth_mode():
    mode = os.getenv("REELDUB_AUTH_MODE", "key")
    if mode not in {"key", "postgres"} or (mode == "postgres" and not uses_postgres()):
        raise HTTPException(503, "Check the server authentication configuration.")
    return mode


def owner(authorization: str = Header(default="")):
    mode = auth_mode()
    scheme, _, key = authorization.partition(" ")
    denied = HTTPException(401, "Enter the connection key for this server.")
    if scheme.lower() != "bearer" or not 32 <= len(key) <= 256:
        raise denied
    if mode == "postgres":
        with db().connection() as conn:
            row = conn.execute("SELECT library_id FROM reeldub.access_keys WHERE token_hash=%s",
                               (hashlib.sha256(key.encode()).digest(),)).fetchone()
        if row:
            return "local:" + str(row[0])
        raise denied
    expected = os.getenv("SELF_HOST_KEY", "")
    if not 32 <= len(expected) <= 256:
        raise HTTPException(503, "Set SELF_HOST_KEY to a private key of 32 to 256 characters.")
    if not secrets.compare_digest(hash_value(key), hash_value(expected)):
        raise denied
    return "self-host"


def limit_writes(user, operation, ceiling):
    minute = int(time.time()) // 60
    bucket = f"{hash_value(user)}/{operation}/{minute}"
    row = db().request_rates.find_one_and_update({"_id": bucket},
        {"$inc": {"count": 1}, "$setOnInsert": {"expires": now() + timedelta(minutes=2)}},
        upsert=True, return_document=ReturnDocument.AFTER)
    if row["count"] > ceiling:
        raise HTTPException(429, "Please wait a minute before trying again.")


def purge_deleted_media(job):
    # Each render has its own immutable GridFS id. Both steps can safely be retried.
    db().playback.delete_many({"job": job["_id"]})
    if "artifact" in job:
        gridfs.GridFS(media_db()).delete(job["artifact"])
        db().jobs.update_one({"_id": job["_id"], "artifact": job["artifact"]}, {"$unset": {"artifact": ""}})


def resume_deletions():
    if uses_postgres():
        db().expire(now())
    sources.sweep()
    # Older workers left undelivered media without an expiry. Allow a week from migration.
    db().jobs.update_many({"state": "done", "deleted": {"$ne": True}, "artifact": {"$exists": True},
                           "media_expired": {"$ne": True}, "media_expires": {"$exists": False}},
                          {"$set": {"awaiting_delivery": True, "media_expires": now() + timedelta(days=7)}})
    # Claim before removing bytes. Renewal can only win while the deadline is in the future.
    db().jobs.update_many({"media_expires": {"$lte": now()}}, {
        "$set": {"media_expired": True, "has_poster": False},
        "$unset": {"media_expires": "", "poster": "", "render_key": ""}})
    for job in db().jobs.find({"artifact": {"$exists": True}, "$or": [
            {"deleted": True, "cache_retained": {"$ne": True}}, {"media_expired": True}]}, {"artifact": 1}):
        purge_deleted_media(job)
    db().jobs.update_many({"deleted": True, "cache_retained": True, "media_expired": True,
                           "artifact": {"$exists": False}}, [{"$project": {
        "owner": 1, "request_id": 1, "request_keys": 1, "state": 1, "deleted": 1}}])


def purge_orphaned_uploads():
    # A worker can exit between GridFS upload and publishing its fenced job update.
    bucket = gridfs.GridFS(media_db())
    old = {"job": {"$type": "string"}, "lease": {"$type": "string"},
           "uploadDate": {"$lt": now() - timedelta(hours=2)}}
    # Check references in the authoritative metadata store. Stream media headers only.
    from itertools import islice
    files = iter(media_db()["fs.files"].find(old, {"job": 1, "lease": 1}))
    while batch := list(islice(files, 100)):
        ids = list({item["job"] for item in batch})
        jobs = {j["_id"]: j for j in db().jobs.find({"_id": {"$in": ids}}, {"artifact": 1, "lease": 1})}
        for item in batch:
            job = jobs.get(item["job"], {})
            if item["_id"] != job.get("artifact") and item["lease"] != job.get("lease"):
                bucket.delete(item["_id"])


def maintain_postgres():
    # Elect one API replica in the local media store without opening a Neon connection.
    schedule = media_db().maintenance
    stamp, lease = now(), uuid4().hex
    key = {"_id": "postgres-hourly"}
    schedule.update_one(key, {"$setOnInsert": {"due": stamp}}, upsert=True)
    claimed = schedule.update_one({**key, "due": {"$lte": stamp}},
                                  {"$set": {"due": stamp + timedelta(minutes=10), "lease": lease}})
    if claimed.matched_count:
        resume_deletions()
        purge_orphaned_uploads()
        due = next_hour(now())
        schedule.update_one({**key, "lease": lease},
                            {"$set": {"due": due}, "$unset": {"lease": ""}})
    else:
        due = schedule.find_one(key, {"due": 1})["due"]
    return max(1, (due - now()).total_seconds())


async def sweep_media():
    ticks = 0
    while True:
        delay = 60
        try:
            if separate_metadata():
                delay = await run_in_threadpool(maintain_postgres)
            else:
                await run_in_threadpool(resume_deletions)
                if ticks % 60 == 0:
                    await run_in_threadpool(purge_orphaned_uploads)
            ticks += 1
        except Exception:
            logging.exception("ReelDub media cleanup will retry")
        await asyncio.sleep(delay)


@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(setup_indexes)
    tasks = [asyncio.create_task(sweep_media())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="ReelDub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


app.include_router(web_router)


@app.exception_handler(ConnectionFailure)
@app.exception_handler(ExecutionTimeout)
@app.exception_handler(WTimeoutError)
@app.exception_handler(OperationalError)
@app.exception_handler(PoolTimeout)
async def database_unavailable(request, error):
    return JSONResponse({"detail":"ReelDub is temporarily unavailable. Please try again later."}, status_code=503)


@app.get("/live")
def live():
    return {"status": "online"}


@app.get("/health")
def health():
    db().command("ping")
    return {"status": "ready"}


@app.get("/privacy", response_class=PlainTextResponse)
def privacy():
    return """ReelDub runs on the server you connect to. The server downloads the video links you share,
processes your videos and speaker samples, and stores your library. People using the same
connection key share that library. Only process recordings you have permission to dub.
Remove a completed dub from your library to remove the device copy. The server can reuse a
cached render for 24 hours after delivery. Undelivered videos expire after seven days.
"""


def quality_languages():
    best = [code for code, language in LANGUAGES.items() if language.matched]
    fast = ["en"] if os.getenv("REELDUB_KITTEN_MATCH") == "1" else []
    return {"fast": fast, "best": best}


@app.get("/v1/me")
def me(user=Depends(owner)):
    languages = quality_languages()
    return {"library_id": hash_value(user), "batch_progress": True, "video_uploads": True,
            "output_languages": languages["best"], "quality_languages": languages,
            "kitten_match_languages": languages["fast"], "reel_links": sources.reel_links_enabled()}


@app.post("/v1/sources/upload")
async def stage_upload(request: Request, user=Depends(owner)):
    await run_in_threadpool(limit_writes,user,"intake",12)
    if request.headers.get("content-type","").split(";")[0].strip().lower() not in (
        "video/mp4", "video/quicktime", "video/x-m4v", "application/octet-stream"
    ):
        raise HTTPException(415,"Choose an MP4 or MOV file.")
    title=unquote(request.headers.get("x-video-title",""))[:140].strip() or "Imported video"
    async with contextmanager_in_threadpool(sources.admission(user)) as stage:
        with TemporaryFile() as file:
            digest = hashlib.sha256()
            async for part in request.stream():
                if file.tell() + len(part) > sources.MAX_BYTES:
                    raise HTTPException(413, "Choose a file of 80 MiB or less.")
                digest.update(part)
                await run_in_threadpool(file.write, part)
            if not file.tell():
                raise HTTPException(422, "Choose a playable MP4 or MOV.")
            return await run_in_threadpool(sources.upload, stage, user, file, digest.hexdigest(), title)


@app.get("/v1/sources/{source_id}")
def source_info(source_id: str, user=Depends(owner)):
    return sources.public(sources.get(user, source_id))


class Submission(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=8192)
    source_id: str | None = Field(default=None, min_length=24, max_length=24)
    request_id: UUID = Field(default_factory=uuid4)
    target_language: str = Field(default="en", min_length=2, max_length=2)
    quality: Literal["best", "fast"] = "best"

    model_config = {"extra": "forbid"}


def media_available(job):
    expires = job.get("media_expires")
    return bool(job.get("artifact") and not job.get("media_expired") and (expires is None or expires > now()))


def live_media_query(stamp, include_cache=False):
    visibility = {"cache_superseded": {"$ne": True}} if include_cache else {"deleted": {"$ne": True}}
    return {"state": "done", "artifact": {"$exists": True}, **visibility,
            "media_expired": {"$ne": True}, "$or": [
                {"media_expires": {"$exists": False}}, {"media_expires": {"$gt": stamp}}]}


def cache_hit_count(request_id):
    count = {"$ifNull":["$cache_hits",0]}
    replay = {"$in":[{"$literal":request_id},{"$ifNull":["$request_keys",[]]}]}
    return {"$cond":[{"$or":[replay,{"$ne":["$state","done"]}]}, count, {"$add":[count,1]}]}


def renew_media(stamp):
    # A download receipt starts the shorter sliding cache window.
    return {"$cond": [{"$or": [{"$eq": ["$awaiting_delivery", True]}, {"$eq": [{"$type": "$media_expires"}, "missing"]}]}, "$media_expires",
                      {"$max": ["$media_expires", stamp + MEDIA_WINDOW]}]}


def next_position(counter_id="queue"):
    counter = db().counters.find_one_and_update({"_id": counter_id}, {"$inc": {"seq": 1}},
                                                upsert=True, return_document=ReturnDocument.AFTER)
    return counter["seq"]


def summary(job, before=None):
    if job["state"] == "queued" and before is None:
        seq = job.get("queue_seq")
        if seq is None:
            earlier = {"queue_seq": None, "$or": [
                {"created": {"$lt": job["created"]}}, {"created": job["created"], "_id": {"$lt": job["_id"]}}]}
        else:
            earlier = {"$or": [{"queue_seq": None}, {"queue_seq": {"$lt": seq}}]}
        lane = {"$or": [{"priority": {"$gt": job["priority"]}}, {"priority": job["priority"], **earlier}]}
        if job.get("recovery_rank"):
            ahead = {"recovery_rank": 1, **lane}
        else:
            ahead = {"$or": [{"recovery_rank": 1}, {"recovery_rank": {"$ne": 1}, **lane}]}
        front = job.get("queue_front")
        if front is not None:
            ahead = {"queue_front": {"$gt": front}}
        else:
            ahead = {"$or": [{"queue_front": {"$gt": 0}}, {"queue_front": None, **ahead}]}
        before = db().jobs.count_documents({"state": "queued", "deleted": {"$ne": True}, **ahead})
    return dict(id=job["_id"], url=job["url"], state=job["state"], stage=job["stage"], created=job["created"],
                progress=job.get("progress"), estimated_finish=job.get("estimated_finish") if job["state"]=="running" else None,
                last_requested=job.get("last_requested"), started=job.get("started"), finished=job.get("finished"), ahead=before, lane="standard", error=job.get("error"),
                recovery_count=job.get("recovery_count", 0),
                recovery_switched_server=bool(job.get("state") == "running" and job.get("recovery_from_node")
                    and job.get("fleet_slot") and job["fleet_slot"].rsplit(":", 1)[0] != job["recovery_from_node"]),
                attempt=job["attempt"], reported=bool(job.get("report")), media_seconds=job.get("media_seconds"),
                title=job.get("title"), author=job.get("author"), has_poster=job.get("has_poster", False),
                available_on_server=media_available(job), cache_expires=job.get("media_expires"),
                source_id=str(job["source_file"]) if job.get("source_file") else None, trimmed=job.get("trimmed",False),
                voice_mode=job.get("voice_mode"), match_engine=job.get("match_engine"),
                screen_text_count=job.get("screen_text_count"), screen_text_engine=job.get("screen_text_engine"),
                quality=job.get("render_quality"), quality_requested=job.get("quality_requested"),
                quality_fallback=job.get("quality_fallback"),
                render_quality=job.get("render_quality"), lab_mode=job.get("lab_mode"), lab_runtime=job.get("lab_runtime"), voice_batch=job.get("voice_batch"), target_language=job.get("target_language", "en"), source_language=job.get("source_language"), first_caption=job.get("first_caption"))


@app.post("/v1/jobs")
def submit(body: Submission, user=Depends(owner)):
    limit_writes(user, "submit", 90)
    if (body.url is None) == (body.source_id is None):
        raise HTTPException(422, "Provide one video URL or uploaded source.")
    url = canonical_url(body.url) if body.url is not None else None
    target, requested = body.target_language, body.quality
    languages = quality_languages()
    if target not in languages["best"]:
        raise HTTPException(422, "Neither speaker-matching engine supports that output language.")

    request_id = str(body.request_id)
    def prior_request():
        previous = db().jobs.find_one({"owner": user, "$or": [
            {"request_id": request_id}, {"request_keys": request_id}]}, {"poster": 0})
        if previous:
            if previous.get("deleted"):
                raise HTTPException(410, "This dub was removed from the library.")
            same_source = previous["url"] == url if url else str(previous.get("source_file")) == body.source_id
            if (not same_source or previous.get("target_language", "en") != target
                    or previous.get("quality_requested") != requested):
                raise HTTPException(409, "This request was already used for a different dub.")
            return summary(previous)

    if previous := prior_request():
        return previous
    source = sources.get(user, body.source_id) if body.source_id else None
    identity = source["identity"] if source else url
    effective = requested
    fallback = None
    if requested == "fast" and target not in languages["fast"]:
        effective = "best"
        fallback = ("Kitten supports English output. Using Best for this language." if target != "en"
                    else "Kitten is unavailable on this server. Using Best.")
    engine = KITTEN_MATCH if effective == "fast" else None
    stamp = now()
    # Include actual and requested quality. A newly available Kitten worker must
    # not reuse a Qwen fallback when someone requests Fast again.
    cache = {"owner": user, "url": identity, "target_language": target, "voice_mode": "match",
             "quality_requested": requested, "render_quality": effective, "match_engine": engine,
             "screen_text_version": SCREEN_TEXT_VERSION, "caption_filter_version": CAPTION_FILTER_VERSION,
             **live_media_query(stamp, include_cache=True),
             "$and": [{"$or": [{"deleted": {"$ne": True}}, {"cache_retained": True}]}]}
    try:
        cached = db().jobs.find_one_and_update(cache,
            [{"$unset": ["deleted", "cache_retained"]}, {"$set": {"last_requested": stamp,
              "cache_hits": cache_hit_count(request_id), "media_expires": renew_media(stamp), "request_keys": {
                  "$setUnion": [{"$ifNull": ["$request_keys", []]}, [request_id]]}}}],
            sort=[("created", -1)], projection={"poster": 0}, return_document=ReturnDocument.AFTER)
        if cached:
            return summary(cached)
        job = dict(_id=str(uuid4()), owner=user, request_id=request_id, request_keys=[request_id],
                   url=identity, target_language=target, voice_mode="match", quality_requested=requested,
                   render_quality=effective, required_screen_text_version=SCREEN_TEXT_VERSION,
                   required_caption_filter_version=CAPTION_FILTER_VERSION, state="queued", stage="Queued", created=stamp,
                   priority=0, queue_seq=next_position(), attempt=0)
        if engine:
            job["match_engine"] = engine
        if fallback:
            job["quality_fallback"] = fallback
        if source:
            job.update(source_file=source["_id"], quoted_seconds=source["seconds"], title=source["title"])
        ticket = notify(preparing=True)
        if not ticket:
            raise HTTPException(503, "The render queue is temporarily unavailable. Try again shortly.")
        db().jobs.insert_one(job)
    except DuplicateKeyError:
        if previous := prior_request():
            return previous
        raise
    notify(ticket=ticket)
    return summary(job)


def owned(job_id, user, *, poster=False):
    projection = None if poster else {"poster": 0}
    job = db().jobs.find_one({"_id": job_id, "owner": user, "deleted": {"$ne": True}}, projection)
    if not job:
        raise HTTPException(404, "Reel not found.")
    return job


@app.get("/v1/jobs")
def jobs(user=Depends(owner)):
    return library_feed(user)


def library_feed(user, ids=None):
    if uses_postgres():
        return [summary(item, ahead) for item, ahead in db().library(user, ids)]
    query = {"owner": user, "deleted": {"$ne": True}}
    if ids is not None:
        query["_id"] = {"$in": ids}
    feed = db().jobs.aggregate([
        {"$match": query},
        {"$unset": ["poster", "request_keys", "minute_units", "minute_grants"]},
        {"$set": {"activity": {"$ifNull": ["$last_requested", "$created"]}}},
        {"$sort": {"activity": -1, "_id": -1}}, {"$limit": 100}])
    return [summary(j) for j in feed]


class LibraryCheck(BaseModel):
    ids: list[UUID] = Field(max_length=100)


@app.post("/v1/library/progress")
def library_progress(body: LibraryCheck, user=Depends(owner)):
    return library_feed(user, [str(value) for value in body.ids]) if body.ids else []


@app.post("/v1/library/deletions")
def library_deletions(body: LibraryCheck, user=Depends(owner)):
    return [job["_id"] for job in db().jobs.find({
        "_id": {"$in": [str(value) for value in body.ids]}, "owner": user, "deleted": True}, {"_id": 1})]


@app.get("/v1/jobs/{job_id}")
def job_status(job_id: str, user=Depends(owner)):
    return summary(owned(job_id, user))


class QueueMove(BaseModel):
    position: Literal["first", "last"]


@app.post("/v1/jobs/{job_id}/queue")
def move_job(job_id: UUID, body: QueueMove, user=Depends(owner)):
    ident = str(job_id)
    scope = {"_id": ident, "owner": user, "deleted": {"$ne": True}, "state": "queued"}
    current = owned(ident, user)
    if current["state"] != "queued":
        raise HTTPException(409, "This video has already left the queue.")
    if body.position == "first":
        # The counter makes front moves unique across devices and API replicas.
        change = {"$set": {"queue_front": next_position("manual-front")}}
    else:
        # Return to normal FIFO. Future submissions go behind this video.
        change = {"$unset": {"queue_front": "", "recovery_rank": ""},
                  "$set": {"queue_seq": next_position(), "priority": 0}}
    moved = db().jobs.find_one_and_update(scope, change, projection={"poster": 0}, return_document=ReturnDocument.AFTER)
    if not moved:
        raise HTTPException(409, "This video has already left the queue.")
    notify()
    return summary(moved)


@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: UUID, user=Depends(owner)):
    ident = str(job_id)
    # Workers must hold a matching running lease for every heartbeat, publish and
    # recovery. Revoking it in the same write prevents a late result or retry.
    cancelled = db().jobs.find_one_and_update(
        {"_id": ident, "owner": user, "deleted": {"$ne": True}, "state": {"$in": ["queued", "running"]}},
        {"$set": {"state": "cancelled", "stage": "Cancelled", "finished": now()},
         "$unset": dict.fromkeys(("lease", "lease_end", "fleet_slot", "fleet_request", "upload_token",
                                  "upload_lease", "upload_until", "estimated_finish", "progress", "queue_front"), "")},
        projection={"poster": 0}, return_document=ReturnDocument.AFTER)
    if cancelled:
        notify()
        return summary(cancelled)
    current = owned(ident, user)
    if current["state"] == "cancelled":
        return summary(current)
    raise HTTPException(409, "This video already finished processing.")


@app.delete("/v1/jobs/{job_id}", status_code=204)
def delete_dub(job_id: str, user=Depends(owner),
               revision: Annotated[str | None, Header(alias="X-ReelDub-Library-Revision", max_length=64)] = None):
    key = {"_id": job_id, "owner": user}
    if revision is not None:
        # A cached dub keeps its ID when requested again, including by a different device.
        if revision == "original":
            key["last_requested"] = None
        else:
            try:
                timestamp = datetime.fromisoformat(revision)
                if timestamp.tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise HTTPException(422, "Invalid library revision.")
            key["last_requested"] = timestamp
    stamp = now()
    retained = db().jobs.find_one_and_update({**key, **live_media_query(stamp)}, [
        {"$set": {"deleted": True, "cache_retained": True,
                  "media_expires": {"$min": [{"$ifNull": ["$media_expires", stamp + MEDIA_WINDOW]}, stamp + MEDIA_WINDOW]},
                  "play_generation": {"$add": [{"$ifNull": ["$play_generation", 0]}, 1]}}},
        {"$unset": ["report", "awaiting_delivery"]}], projection={"play_generation": 1}, return_document=ReturnDocument.AFTER)
    if retained:
        db().playback.delete_many({"job": job_id, "$or": [{"generation": None},
                                  {"generation": {"$lt": retained["play_generation"]}}]})
        return
    if db().jobs.find_one({**key, "deleted": True}, {"_id": 1}):
        return
    # Revoke reads first. Keep only the quota and idempotency record plus a
    # recoverable reference to the video until GridFS cleanup completes.
    job = db().jobs.find_one_and_update(
        {**key, "deleted": {"$ne": True}, "state": {"$in": ["done", "failed", "cancelled"]},
         "$or": [{"artifact": {"$exists": False}}, {"media_expired": True}, {"media_expires": {"$lte": stamp}}]},
        [{"$project": {"owner": 1, "request_id": 1, "request_keys": 1, "state": 1,
                       "artifact": 1, "deleted": {"$literal": True}}}],
        return_document=ReturnDocument.AFTER)
    if not job:
        current = owned(job_id, user)
        if revision is not None and current.get("last_requested") != key["last_requested"]:
            raise HTTPException(412, "This dub was requested again. Its new library entry was kept.")
        raise HTTPException(409, "Wait for this translation to finish before deleting it.")
    purge_deleted_media(job)


def requested_media(job_id, user):
    stamp = now()
    job = db().jobs.find_one_and_update({"_id": job_id, "owner": user, **live_media_query(stamp)},
        [{"$set": {"media_expires": renew_media(stamp)}}], projection={"poster": 0}, return_document=ReturnDocument.AFTER)
    if not job:
        job = owned(job_id, user)
        if job["state"] != "done":
            raise HTTPException(409, "The render is not ready.")
        raise HTTPException(410, "The server copy expired. Translate this Reel again.")
    return job


class Delivery(BaseModel):
    bytes: int = Field(gt=0)


@app.post("/v1/jobs/{job_id}/received", status_code=204)
def received(job_id: str, body: Delivery, user=Depends(owner)):
    job = owned(job_id, user)
    if not media_available(job):
        raise HTTPException(410, "This video is unavailable.")
    with open_video(job) as file:
        if file.length != body.bytes:
            raise HTTPException(409, "The downloaded size does not match the render.")
    # Repeated receipts, including after an interrupted response, do not extend the timer.
    db().jobs.update_one({"_id": job_id, "owner": user, "artifact": job["artifact"],
                          "deleted": {"$ne": True}, "media_expired": {"$ne": True},
                          "$or": [{"awaiting_delivery": True}, {"media_expires": {"$exists": False}}]},
                         {"$set": {"media_expires": now() + MEDIA_WINDOW}, "$unset": {"awaiting_delivery": ""}})


def open_video(job):
    if not media_available(job):
        raise HTTPException(410, "The server copy expired.")
    try:
        return gridfs.GridFS(media_db()).get(job["artifact"])
    except gridfs.NoFile:
        # A deletion may finish after the owner check and before the file opens.
        raise HTTPException(404, "Video not found.")


@app.get("/v1/jobs/{job_id}/video")
def video(job_id: str, user=Depends(owner)):
    job = requested_media(job_id, user)
    file = open_video(job)
    def chunks():
        try:
            while part := file.read(256 * 1024):
                yield part
        finally:
            file.close()
    return StreamingResponse(chunks(), media_type="video/mp4", headers={
        "Content-Length": str(file.length), "Cache-Control": "private, no-store"})


@app.get("/v1/jobs/{job_id}/poster")
def poster(job_id: str, user=Depends(owner)):
    job = owned(job_id, user, poster=True)
    image = job.get("poster") if media_available(job) else None
    if image is None and media_available(job) and job.get("has_poster"):
        file = media_db()["fs.files"].find_one({"_id": job["artifact"]}, {"poster": 1})
        image = file.get("poster") if file else None
    if not image:
        raise HTTPException(404, "This video has no poster.")
    return Response(bytes(image), media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@app.post("/v1/jobs/{job_id}/playback")
def playback(job_id: str, user=Depends(owner), authorization: str = Header(default="")):
    job = requested_media(job_id, user)
    ticket = secrets.token_urlsafe(36)
    grant = {"_id": hash_value(ticket), "job": job_id, "owner": user,
             "generation": job.get("play_generation", 0), "expires": now() + timedelta(minutes=30)}
    if auth_mode() == "postgres":
        key = authorization.partition(" ")[2]
        with db().connection() as conn:
            row = conn.execute("SELECT generation FROM reeldub.access_keys WHERE library_id=%s AND token_hash=%s",
                               (UUID(user.removeprefix("local:")), hashlib.sha256(key.encode()).digest())).fetchone()
        if not row:
            raise HTTPException(401, "This connection key has changed.")
        grant["access_generation"] = str(row[0])
    db().playback.insert_one(grant)
    return {"ticket": ticket}


def playback_grant(digest, job_id, stamp):
    if auth_mode() == "postgres":
        return db().playback_grant(digest, job_id, stamp)
    grant = db().playback.find_one({"_id": digest, "job": job_id, "expires": {"$gt": stamp}})
    if grant and grant.get("owner") != "self-host":
        return None
    return grant


@app.api_route("/v1/play/{job_id}/{ticket}", methods=["GET", "HEAD"])
def stream(job_id: str, ticket: str, request: Request, range: str | None = Header(default=None)):
    grant = playback_grant(hash_value(ticket), job_id, now())
    if not grant:
        raise HTTPException(404, "Playback link expired.")
    job = owned(job_id, grant["owner"])
    if job.get("play_generation", 0) != grant.get("generation", 0):
        raise HTTPException(404, "Playback link expired.")
    file = open_video(job)
    total = file.length
    start, end = 0, total - 1
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}
    if range:
        match = re.fullmatch(r"bytes=([0-9]{0,16})-([0-9]{0,16})", range)
        if not match or not any(match.groups()):
            file.close()
            raise HTTPException(416, headers={"Content-Range": f"bytes */{total}"})
        left, right = match.groups()
        if left:
            start = int(left)
            end = min(int(right), end) if right else end
        else:
            start = max(0, total - int(right))
        if start > end:
            file.close()
            raise HTTPException(416, headers={"Content-Range": f"bytes */{total}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    length = end - start + 1
    headers["Content-Length"] = str(length)
    status = 206 if range else 200
    if request.method == "HEAD":
        file.close()
        return Response(media_type="video/mp4", headers=headers, status_code=status)
    def chunks():
        try:
            file.seek(start)
            remaining = length
            while remaining:
                chunk = file.read(min(remaining, 256 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            file.close()
    return StreamingResponse(chunks(), media_type="video/mp4", headers=headers, status_code=status)


class Issue(BaseModel):
    category: Literal["Translation", "Voice", "Timing", "Background", "Other"]
    note: str = Field(min_length=1, max_length=2000)


@app.post("/v1/jobs/{job_id}/report")
def report(job_id: str, issue: Issue, user=Depends(owner)):
    note = issue.note.strip()
    if not note:
        raise HTTPException(422, "Add a note.")
    result = db().jobs.update_one({"_id": job_id, "owner": user, "deleted": {"$ne": True}}, {"$set": {
        "report": {"category": issue.category, "note": note, "submitted": now(), "status": "open"}}})
    if not result.matched_count:
        raise HTTPException(404, "Reel not found.")
    return {"reported": True}
