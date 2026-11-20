"""Private, expiring source files for duration quotes and their confirmed jobs."""

from contextlib import contextmanager
from datetime import timedelta
import os
from tempfile import TemporaryFile
from urllib.parse import unquote

import gridfs
import requests
from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .db import media_db, db, now

MAX_BYTES = 80 * 1024 * 1024
INTAKE = os.getenv("REELDUB_INTAKE_URL", "http://intake:8310")
WINDOW = timedelta(hours=24)


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
    return doc


@contextmanager
def admission(user):
    total = sum(x.get("bytes", 0) for x in db().sources.find({"owner": user}, {"bytes": 1}))
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


def save(stage, user, file, identity, seconds, title, author=None):
    file.seek(0, 2)
    size = file.tell()
    if not 0 < size <= MAX_BYTES or not 1 <= seconds <= 600:
        raise HTTPException(422, "Choose a video within the import limits.")
    # A ready source already contains immutable bytes. Never trust a client-supplied identifier or duration.
    cached = db().sources.find_one_and_update(
        {"owner": user, "identity": identity, "ready": True, "expires": {"$gt": now()}},
        {"$max": {"expires": now() + WINDOW}},
        return_document=ReturnDocument.AFTER,
    )
    if cached:
        return public(cached)
    file.seek(0)
    gridfs.GridFS(media_db()).put(file, _id=stage, filename="source.mp4")
    doc = db().sources.find_one_and_update(
        {"_id": stage, "owner": user, "slot": {"$exists": True}},
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
            "$unset": {"slot": ""},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        gridfs.GridFS(media_db()).delete(stage)
        raise HTTPException(409, "Import expired. Try again.")
    return public(doc)


def upload(stage, user, file, digest, title):
    file.seek(0)
    try:
        with requests.post(INTAKE + "/inspect", data=file, timeout=(4, 35), allow_redirects=False) as r:
            check(r)
            seconds = r.json()["seconds"]
    except (requests.RequestException, ValueError, KeyError):
        raise HTTPException(503, "Video intake is temporarily unavailable.")
    return save(stage, user, file, "upload:" + digest, seconds, title)


def reel(user, url):
    cached = db().sources.find_one_and_update(
        {"owner": user, "identity": url, "ready": True, "expires": {"$gt": now()}},
        {"$max": {"expires": now() + WINDOW}},
        return_document=ReturnDocument.AFTER,
    )
    if cached:
        return public(cached)
    with admission(user) as stage, TemporaryFile() as file:
        try:
            with requests.post(
                INTAKE + "/reel", json={"url": url}, stream=True, timeout=(4, 155), allow_redirects=False
            ) as r:
                check(r)
                seconds = int(r.headers["X-Duration"])
                title, author = (unquote(r.headers.get(k, "")) for k in ("X-Title", "X-Author"))
                for part in r.iter_content(256 * 1024):
                    if file.tell() + len(part) > MAX_BYTES:
                        raise HTTPException(413, "Choose a video under 80 MiB.")
                    file.write(part)
        except (requests.RequestException, ValueError, KeyError):
            raise HTTPException(503, "The Reel could not be prepared. Try again.")
        return save(stage, user, file, url, seconds, title or "Instagram Reel", author)


def sweep():
    for rec in db().sources.find({"expires": {"$lte": now()}}, {"expires": 1}).limit(100):
        if db().jobs.find_one(
            {"source_file": rec["_id"], "state": {"$in": ["queued", "running"]}, "deleted": {"$ne": True}},
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
