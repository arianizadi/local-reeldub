import hashlib
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

import gridfs
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from .db import db, now, setup_indexes

LIMITS = {"free": 10, "paid": 50, "admin": None}
URL = re.compile(r"https://(?:(?:www|m)\.)?instagram\.com/reels?/([A-Za-z0-9_-]{5,32})/?(?:[?#][^\s]*)?", re.I)
apple_keys = jwt.PyJWKClient("https://appleid.apple.com/auth/keys", timeout=8)


def hash_value(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_url(value):
    match = URL.fullmatch(value.strip())
    if not match:
        raise HTTPException(422, "Paste a link to an individual Instagram Reel.")
    return f"https://www.instagram.com/reel/{match[1]}/"


def owner(authorization: str = Header(default="")):
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not 20 <= len(key) <= 256:
        raise HTTPException(401, "Connect a server or sign in.")
    local = os.getenv("SELF_HOST_KEY", "")
    if len(local) >= 32 and secrets.compare_digest(hash_value(key), hash_value(local)):
        return "self-host"
    record = db().sessions.find_one({"_id": hash_value(key), "expires": {"$gt": now()}})
    if not record:
        raise HTTPException(401, "Your connection has expired. Sign in again.")
    return record["owner"]


def tier(user):
    if user == "self-host" or user == "apple:" + os.getenv("ADMIN_APPLE_SUB", "disabled"):
        return "admin"
    account = db().accounts.find_one({"_id": user}, {"tier": 1}) or {}
    return account.get("tier", "free")


def quota(user, plan, stamp):
    day = stamp.date().isoformat()
    used = db().jobs.count_documents({"owner": user, "day": day, "state": {"$ne": "failed"}})
    limit = LIMITS[plan]
    reset = stamp.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return dict(tier=plan, used=used, limit=limit, resets=reset)


@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(setup_indexes)
    yield


app = FastAPI(title="ReelDub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health():
    db().command("ping")
    return {"status": "ready"}


@app.get("/v1/me")
def me(user=Depends(owner)):
    result = quota(user, tier(user), now())
    result["email"] = (db().accounts.find_one({"_id": user}, {"email": 1}) or {}).get("email")
    return result


@app.post("/v1/apple/challenge")
def challenge(request: Request):
    if not os.getenv("APPLE_CLIENT_ID"):
        raise HTTPException(503, "This server uses a connection key. Apple sign-in requires a configured app ID.")
    stamp = now()
    client_ip = request.client.host
    if os.getenv("TRUST_TUNNEL_IP") == "1":
        client_ip = request.headers.get("CF-Connecting-IP") or client_ip
    ip = hash_value(client_ip)
    if db().challenges.count_documents({"ip": ip, "expires": {"$gt": stamp}}, limit=20) >= 20:
        raise HTTPException(429, "Try signing in again later.")
    value = secrets.token_urlsafe(32)
    db().challenges.insert_one({"_id": hash_value(value), "ip": ip, "expires": stamp + timedelta(minutes=5)})
    return {"challenge": value}


class AppleLogin(BaseModel):
    identity_token: str = Field(max_length=12000)
    challenge: str = Field(min_length=32, max_length=100)


@app.post("/v1/apple/session")
def apple(login: AppleLogin):
    audience = os.getenv("APPLE_CLIENT_ID")
    if not audience:
        raise HTTPException(503, "Apple sign-in is not configured.")
    nonce = hash_value(login.challenge)
    challenge_query = {"_id": nonce, "expires": {"$gt": now()}}
    if not db().challenges.find_one(challenge_query, {"_id": 1}):
        raise HTTPException(401, "The sign-in challenge expired. Try again.")
    try:
        key = apple_keys.get_signing_key_from_jwt(login.identity_token).key
        claims = jwt.decode(login.identity_token, key, algorithms=["RS256"], audience=audience,
                            issuer="https://appleid.apple.com", options={"require": ["exp", "iat", "sub", "nonce"]})
    except jwt.PyJWTError:
        raise HTTPException(401, "Apple could not verify this sign-in.")
    if claims["nonce"] != nonce or not db().challenges.find_one_and_delete({"_id": nonce, "expires": {"$gt": now()}}):
        raise HTTPException(401, "The sign-in challenge expired.")
    user = "apple:" + claims["sub"]
    admin_email = os.getenv("ADMIN_APPLE_EMAIL", "").strip().casefold()
    verified = claims.get("email_verified") in (True, "true")
    email = claims.get("email")
    update = {"$set": {"last_sign_in": now()}, "$setOnInsert": {"created": now()}}
    if verified and isinstance(email, str):
        update["$set"]["email"] = email
    if admin_email and verified and isinstance(email, str) and email.casefold() == admin_email:
        update["$set"]["tier"] = "admin"
    db().accounts.update_one({"_id": user}, update, upsert=True)
    token = secrets.token_urlsafe(48)
    db().sessions.insert_one({"_id": hash_value(token), "owner": user,
                              "expires": now() + timedelta(days=30)})
    return {"token": token}


@app.delete("/v1/session", status_code=204)
def logout(authorization: str = Header(), user=Depends(owner)):
    db().sessions.delete_one({"_id": hash_value(authorization.split(" ", 1)[1]), "owner": user})


class Submission(BaseModel):
    url: str = Field(max_length=2048)
    request_id: UUID


def summary(job):
    before = None
    if job["state"] == "queued":
        before = db().jobs.count_documents({"state": "queued", "$or": [
            {"priority": {"$gt": job["priority"]}},
            {"priority": job["priority"], "created": {"$lt": job["created"]}},
            {"priority": job["priority"], "created": job["created"], "_id": {"$lt": job["_id"]}},
        ]})
    return dict(id=job["_id"], url=job["url"], state=job["state"], stage=job["stage"], created=job["created"],
                started=job.get("started"), finished=job.get("finished"), ahead=before, error=job.get("error"),
                attempt=job["attempt"], reported=bool(job.get("report")), media_seconds=job.get("media_seconds"),
                title=job.get("title"), author=job.get("author"), has_poster=job.get("has_poster", False))


@app.post("/v1/jobs")
def submit(body: Submission, user=Depends(owner)):
    url = canonical_url(body.url)
    key = {"owner": user, "request_id": str(body.request_id)}
    plan = tier(user)
    stamp = now()
    job = dict(_id=str(uuid4()), **key, url=url, state="queued", stage="Queued", created=stamp,
               priority=int(plan != "free"), day=stamp.date().isoformat(), attempt=0)
    # The quota slot and job share one document. Unique indexes serialize concurrent submissions.
    for slot in range(LIMITS[plan] or 1):
        if LIMITS[plan] is not None:
            job["slot"] = slot
        try:
            db().jobs.insert_one(job)
            return summary(job)
        except DuplicateKeyError:
            previous = db().jobs.find_one(key)
            if previous:
                if previous["url"] != url:
                    raise HTTPException(409, "This request was already used for another Reel.")
                return summary(previous)
    raise HTTPException(429, "Daily translation limit reached. It resets at midnight UTC.")


def owned(job_id, user, *, poster=False):
    projection = None if poster else {"poster": 0}
    job = db().jobs.find_one({"_id": job_id, "owner": user}, projection)
    if not job:
        raise HTTPException(404, "Reel not found.")
    return job


@app.get("/v1/jobs")
def jobs(user=Depends(owner)):
    return [summary(j) for j in db().jobs.find({"owner": user}, {"poster": 0}).sort("created", -1).limit(100)]


@app.get("/v1/jobs/{job_id}")
def job_status(job_id: str, user=Depends(owner)):
    return summary(owned(job_id, user))


@app.get("/v1/jobs/{job_id}/video")
def video(job_id: str, user=Depends(owner)):
    job = owned(job_id, user)
    if job["state"] != "done":
        raise HTTPException(409, "The render is not ready.")
    file = gridfs.GridFS(db()).get(job["artifact"])
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
    image = owned(job_id, user, poster=True).get("poster")
    if not image:
        raise HTTPException(404, "This Reel has no poster.")
    return Response(bytes(image), media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@app.post("/v1/jobs/{job_id}/playback")
def playback(job_id: str, user=Depends(owner)):
    job = owned(job_id, user)
    if job["state"] != "done":
        raise HTTPException(409, "The render is not ready.")
    ticket = secrets.token_urlsafe(36)
    db().playback.insert_one({"_id": hash_value(ticket), "job": job_id, "owner": user,
                              "expires": now() + timedelta(minutes=30)})
    return {"ticket": ticket}


@app.api_route("/v1/play/{job_id}/{ticket}", methods=["GET", "HEAD"])
def stream(job_id: str, ticket: str, request: Request, range: str | None = Header(default=None)):
    grant = db().playback.find_one({"_id": hash_value(ticket), "job": job_id, "expires": {"$gt": now()}})
    if not grant:
        raise HTTPException(404, "Playback link expired.")
    job = owned(job_id, grant["owner"])
    file = gridfs.GridFS(db()).get(job["artifact"])
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
    owned(job_id, user)
    note = issue.note.strip()
    if not note:
        raise HTTPException(422, "Add a note.")
    db().jobs.update_one({"_id": job_id, "owner": user}, {"$set": {
        "report": {"category": issue.category, "note": note, "submitted": now(), "status": "open"}}})
    return {"reported": True}
