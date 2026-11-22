"""Private, expiring source files for duration quotes and their confirmed jobs."""

from contextlib import contextmanager, nullcontext
from datetime import timedelta
import os
from threading import Condition, Lock
import time
from tempfile import TemporaryFile
from urllib.parse import unquote, urlsplit
from weakref import WeakValueDictionary

import gridfs
import requests
from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .db import media_db, db, now
from .source import reel_url

MAX_BYTES = 80 * 1024 * 1024
DAILY_REEL_BYTES = 250 * 1024 * 1024
INTAKE = os.getenv("REELDUB_INTAKE_URL", "http://intake:8310")
WINDOW = timedelta(hours=24)
PREPARATION_LEASE = timedelta(minutes=4)
PREPARATION_WAIT_SECONDS = 168
_preparations = WeakValueDictionary()
_preparations_lock = Lock()


def intake_tls():
    ca, cert, key = (os.getenv("REELDUB_INTAKE_" + name) for name in ("CA", "CERT", "KEY"))
    if not any((ca, cert, key)):
        return {}
    if not all((ca, cert, key)) or urlsplit(INTAKE).scheme != "https":
        raise HTTPException(503, "Private video intake is not configured correctly.")
    return {"verify": ca, "cert": (cert, key)}


@contextmanager
def intake_post(path, **kwargs):
    with requests.Session() as client:
        client.trust_env = False
        with client.post(INTAKE + path, allow_redirects=False, **intake_tls(), **kwargs) as reply:
            yield reply


def public(doc):
    return {"id": str(doc["_id"]), "seconds": doc["seconds"], "title": doc["title"]}


def get(user, value, renew=True):
    if not ObjectId.is_valid(value):
        raise HTTPException(404, "Source unavailable. Import the video again.")
    key = {"_id": ObjectId(value), "owner": user, "ready": True, "expires": {"$gt": now()}}
    doc = (
        db().sources.find_one_and_update(
            key, {"$max": {"expires": now() + WINDOW}}, return_document=ReturnDocument.AFTER
        )
        if renew
        else db().sources.find_one(key)
    )
    if not doc:
        raise HTTPException(404, "Source unavailable. Import the video again.")
    if doc.get("bytes", 0) > MAX_BYTES:
        raise HTTPException(413, "Choose a video under 80 MiB.")
    return doc


@contextmanager
def admission(user):
    total = sum(x.get("bytes", 0) for x in db().sources.find(
        {"owner": user, "confirmed_daily": {"$ne": True}}, {"bytes": 1}))
    if total > 2 * 1024**3 - MAX_BYTES:
        raise HTTPException(429, "Your temporary import storage is full. Try again tomorrow.")
    stage = ObjectId()
    for slot in range(3):
        try:
            db().sources.insert_one(
                {"_id": stage, "owner": user, "slot": slot, "expires": now() + timedelta(minutes=10)}
            )
            break
        except DuplicateKeyError:
            continue
    else:
        raise HTTPException(429, "Finish one of your current imports first.")
    try:
        yield stage
    finally:
        if db().sources.delete_one({"_id": stage, "ready": {"$ne": True}}).deleted_count:
            gridfs.GridFS(media_db()).delete(stage)


def check(response):
    if response.status_code != 200:
        if response.status_code in (413, 422, 429):
            try:
                message = response.json()["detail"]
            except (ValueError, KeyError):
                message = "The source could not be prepared."
            raise HTTPException(response.status_code, message)
        raise HTTPException(503, "Video intake is temporarily unavailable.")


def save(stage, user, file, identity, seconds, title, author=None, *, max_bytes=MAX_BYTES, reel_slot=None):
    file.seek(0, 2)
    size = file.tell()
    if not 0 < size <= max_bytes or not 1 <= seconds <= 600:
        raise HTTPException(422, "Choose a video within the import limits.")
    # A ready source already contains immutable bytes. Never trust a client-supplied identifier or duration.
    if not reel_slot:
        cached = cached_source(user, identity, max_bytes)
        if cached:
            return cached
    file.seek(0)
    gridfs.GridFS(media_db()).put(file, _id=stage, filename="source.mp4")
    doc = db().sources.find_one_and_update(
        {"_id": stage, "owner": user, "slot": reel_slot or {"$exists": True}, "retiring": {"$ne": True}},
        {
            "$set": {
                "identity": identity,
                "seconds": seconds,
                "title": title or "Imported video",
                "author": author,
                "ready": True,
                "bytes": size,
                "expires": now() + WINDOW,
            },
            **({} if reel_slot else {"$unset": {"slot": ""}}),
        },
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        gridfs.GridFS(media_db()).delete(stage)
        if reel_slot:
            raise HTTPException(503, "The source preparation expired. Try again.")
        raise HTTPException(409, "Import expired. Try again.")
    return doc


def upload(stage, user, file, digest, title):
    file.seek(0)
    try:
        with intake_post("/inspect", data=file, timeout=(4, 35)) as r:
            check(r)
            seconds = r.json()["seconds"]
    except (requests.RequestException, ValueError, KeyError):
        raise HTTPException(503, "Video intake is temporarily unavailable.")
    return public(save(stage, user, file, "upload:" + digest, seconds, title))


def cached_source(user, identity, max_bytes):
    doc = db().sources.find_one_and_update(
        {"owner": user, "identity": identity, "ready": True, "expires": {"$gt": now()}},
        {"$max": {"expires": now() + WINDOW}},
        return_document=ReturnDocument.AFTER,
    )
    if doc and doc.get("bytes", 0) > max_bytes:
        raise HTTPException(413, f"Choose a video under {max_bytes // (1024 * 1024)} MiB.")
    return doc


def fetch_reel(stage, user, url, slot, daily):
    max_bytes = DAILY_REEL_BYTES if daily else MAX_BYTES
    deadline = time.monotonic() + 155
    with TemporaryFile() as file:
        try:
            with intake_post(
                "/reel", json={"url": url, "daily": daily}, stream=True, timeout=(4, 155)
            ) as r:
                check(r)
                seconds = int(r.headers["X-Duration"])
                if not 1 <= seconds <= 600:
                    raise HTTPException(422, "Choose a video up to ten minutes long.")
                declared = int(r.headers["Content-Length"]) if "Content-Length" in r.headers else None
                if declared is not None and not 0 < declared <= max_bytes:
                    raise HTTPException(413, f"Choose a video under {max_bytes // (1024 * 1024)} MiB.")
                title, author = (unquote(r.headers.get(k, "")) for k in ("X-Title", "X-Author"))
                for part in r.iter_content(256 * 1024):
                    if time.monotonic() >= deadline:
                        raise HTTPException(503, "Video intake timed out. Try again.")
                    if file.tell() + len(part) > max_bytes:
                        raise HTTPException(413, f"Choose a video under {max_bytes // (1024 * 1024)} MiB.")
                    file.write(part)
                if declared is not None and file.tell() != declared:
                    raise HTTPException(503, "The source transfer was interrupted.")
        except (requests.RequestException, ValueError, KeyError):
            raise HTTPException(503, "The Reel could not be prepared. Try again.")
        pin = db().sources.update_one({"_id": stage, "slot": slot, "owner": user, "retiring": {"$ne": True},
                                      "expires": {"$gt": now()}}, {"$set": {"expires": now() + PREPARATION_LEASE}})
        if not pin.matched_count:
            raise HTTPException(503, "The source preparation expired. Try again.")
        return save(stage, user, file, url, seconds, title or "Instagram Reel", author,
                    max_bytes=max_bytes, reel_slot=slot)


def prepare_reel(user, url, *, daily=False, job_fence=None):
    url = reel_url(url)
    # String slots share the existing unique (owner, slot) index with numeric import permits.
    # Keep this slot on the ready record so a losing or late writer cannot publish a duplicate.
    slot = "reel:" + url
    key = {"owner": user, "slot": slot}
    with _preparations_lock:
        ready = _preparations.setdefault((user, url), Condition())
    deadline = time.monotonic() + PREPARATION_WAIT_SECONDS
    delay = 0.5
    while True:
        cached = cached_source(user, url, DAILY_REEL_BYTES if daily else MAX_BYTES)
        if cached:
            return cached
        stamp = now()
        holder = db().sources.find_one(key, {"expires": 1})
        if holder:
            if holder["expires"] <= stamp:
                db().sources.update_one({**key, "_id": holder["_id"], "expires": holder["expires"]},
                                        {"$unset": {"slot": ""}})
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPException(503, "This Reel is still being prepared. Try again shortly.")
            with ready:
                ready.wait(min(delay, remaining))
            delay = min(8, delay * 2)
            continue
        stage = ObjectId()
        try:
            db().sources.insert_one({**key, "_id": stage, "confirmed_daily": daily,
                                     "expires": stamp + PREPARATION_LEASE})
        except DuplicateKeyError:
            continue
        try:
            # Register the pending source before rechecking admission. Concurrent account erasure
            # can now remove the placeholder and fence the later publication of its bytes.
            if job_fence and not db().jobs.find_one({**job_fence, "lease_end": {"$gt": now()}}, {"_id": 1}):
                raise HTTPException(409, "This worker no longer owns the job.")
            if db().account_deletions.find_one({"_id": user}, {"_id": 1}):
                raise HTTPException(409, "This account is closing.")
            # Confirmed daily work was admitted against its own daily quota. Creator previews
            # still need the pre-purchase import allowance, just once per cold source.
            with nullcontext() if daily else admission(user):
                return fetch_reel(stage, user, url, slot, daily)
        finally:
            if db().sources.delete_one({"_id": stage, "ready": {"$ne": True}}).deleted_count:
                gridfs.GridFS(media_db()).delete(stage)
            with ready:
                ready.notify_all()


def reel(user, url):
    return public(prepare_reel(user, url))


def for_job(job):
    """Resolve a private input for the current worker lease, exclusively from server metadata."""
    fence = {"_id": job["_id"], "owner": job["owner"], "lease": job["lease"], "state": "running",
             "deleted": {"$ne": True}}
    current = db().jobs.find_one({**fence, "lease_end": {"$gt": now()}},
                                 {"url": 1, "source_file": 1, "staged_source": 1})
    if not current:
        raise HTTPException(409, "This worker no longer owns the job.")
    source_id = current.get("source_file") or current.get("staged_source")
    doc = None
    if source_id:
        doc = db().sources.find_one_and_update(
            {"_id": source_id, "owner": job["owner"], "identity": current["url"], "ready": True},
            {"$max": {"expires": now() + WINDOW}}, return_document=ReturnDocument.AFTER)
    if not doc:
        if current.get("source_file"):
            raise HTTPException(410, "The confirmed source is no longer available.")
        doc = prepare_reel(job["owner"], current["url"], daily=True, job_fence=fence)
    attached = db().jobs.update_one({**fence, "lease_end": {"$gt": now()}}, {"$set": {
        "staged_source": doc["_id"], "staged_lease": job["lease"], "title": doc["title"], "author": doc.get("author")}})
    if not attached.matched_count:
        raise HTTPException(409, "This worker no longer owns the job.")
    return doc


def sweep():
    for rec in db().sources.find({"expires": {"$lte": now()}}, {"expires": 1}).limit(100):
        if db().jobs.find_one(
            {"$or": [{"source_file": rec["_id"]}, {"staged_source": rec["_id"]}],
             "state": {"$in": ["queued", "running"]}, "deleted": {"$ne": True}},
            {"_id": 1},
        ):
            db().sources.update_one(
                {"_id": rec["_id"], "expires": rec["expires"]},
                {"$set": {"expires": now() + timedelta(hours=1)}},
            )
        else:
            marker = {"_id": rec["_id"], "expires": rec["expires"]}
            if db().sources.update_one(marker, {"$set": {"ready": False, "retiring": True}}).matched_count:
                gridfs.GridFS(media_db()).delete(rec["_id"])
                db().sources.delete_one({**marker, "retiring": True})
