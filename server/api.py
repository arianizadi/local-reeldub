import asyncio
import hashlib
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager, nullcontext
from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4
from tempfile import TemporaryFile
from urllib.parse import unquote
import time

import gridfs
import jwt
from psycopg import OperationalError
from psycopg_pool import PoolTimeout
from fastapi.responses import JSONResponse
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from .wakeup import next_hour, notify
from .db import uses_postgres, media_db, db, now, setup_indexes
from . import console, checkout, erasure, store, store_history, minutes, sources
from .source import reel_url
from .languages import default_render_quality, outputs

MEDIA_WINDOW = timedelta(hours=24)
LIMITS = {"free": 5, "paid": 50, "admin": None}
BETA_REVIEWER = "review:apple-beta"
apple_keys = jwt.PyJWKClient("https://appleid.apple.com/auth/keys", timeout=8)


def hash_value(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_url(value):
    try:
        return reel_url(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def owner(authorization: str = Header(default="")):
    return session_owner(authorization)


def restricted_owner(authorization: str = Header(default="")):
    return session_owner(authorization, allow_banned=True)


def session_owner(authorization, allow_banned=False):
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not 20 <= len(key) <= 256:
        raise HTTPException(401, "Connect a server or sign in.")
    mode = os.getenv("REELDUB_ACCESS_MODE", "hosted")
    if mode == "operator":
        local = os.getenv("SELF_HOST_KEY", "")
        if len(local) >= 32 and secrets.compare_digest(hash_value(key), hash_value(local)):
            return "self-host"
        raise HTTPException(401, "A private server connection key is required.")
    if mode != "hosted":
        raise HTTPException(503, "Server access is not configured.")
    digest, stamp = hash_value(key), now()
    if uses_postgres():
        record = db().live_session(digest, stamp)
    else:
        record = db().sessions.find_one({"_id": digest, "expires": {"$gt": stamp}}, {"owner": 1, "purpose": 1})
    if record and record["owner"] == BETA_REVIEWER and record.get("purpose") == "beta-review":
        return BETA_REVIEWER
    if not record or record.get("purpose") or not (record.get("owner") or "").startswith("apple:"):
        raise HTTPException(401, "Your connection has expired. Sign in again.")
    closing = record["closing"] if uses_postgres() else erasure.closing(record["owner"])
    if closing:
        raise HTTPException(401, "Your connection has expired. Sign in again.")
    if not allow_banned:
        banned = record.get("banned") if uses_postgres() else db().accounts.find_one({"_id":record["owner"], "banned":True}, {"_id":1})
        if banned:
            raise HTTPException(403, "This account is suspended.")
    return record["owner"]


def limit_writes(user, operation, ceiling):
    minute = int(time.time()) // 60
    bucket = f"{hash_value(user)}/{operation}/{minute}"
    row = db().request_rates.find_one_and_update({"_id": bucket},
        {"$inc": {"count": 1}, "$setOnInsert": {"expires": now() + timedelta(minutes=2)}},
        upsert=True, return_document=ReturnDocument.AFTER)
    if row["count"] > ceiling:
        raise HTTPException(429, "Please wait a minute before trying again.")


def tier(user, account=None):
    if user == BETA_REVIEWER:
        return "free"
    if user == "self-host" or user == "apple:" + os.getenv("ADMIN_APPLE_SUB", "disabled"):
        return "admin"
    if account is None:
        account = db().accounts.find_one({"_id": user}, {"tier": 1}) or {}
    manual = account.get("tier", "free")
    return "paid" if manual == "free" and store.premium(user) else manual


def beta_invite_id(email):
    return hash_value("reeldub/beta/" + email.strip().casefold())


def daily_limit(user, plan):
    if user == BETA_REVIEWER:
        return LIMITS["free"]
    if plan == "admin":
        return None
    base = LIMITS["free" if minutes.enabled() else plan]
    grant = db().beta_allowances.find_one({"owner": user}, {"limit": 1}, sort=[("limit", -1)])
    return max(base, grant["limit"]) if grant else base


def quota(user, plan, stamp):
    day = stamp.date().isoformat()
    if minutes.enabled():
        used = db().jobs.count_documents({"owner":user,"day":day,"slot":{"$exists":True}})
    else:
        used = db().jobs.count_documents({"owner": user, "day": day, "state": {"$ne": "failed"}, "extra_ticket": {"$exists": False}})
    limit = daily_limit(user, plan)
    reset = stamp.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return dict(tier="beta" if plan == "free" and limit > LIMITS["free"] else plan, used=used, limit=limit, resets=reset)


def purge_deleted_media(job):
    # Each render has its own immutable GridFS id. Both steps can safely be retried.
    db().playback.delete_many({"job": job["_id"]})
    if "artifact" in job:
        gridfs.GridFS(media_db()).delete(job["artifact"])
        db().jobs.update_one({"_id": job["_id"], "artifact": job["artifact"]}, {"$unset": {"artifact": ""}})


def resume_deletions():
    if uses_postgres():
        db().expire(now())
    erasure.resume()
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
    # Keep only the deletion marker, idempotency and billing fields once the cache is gone.
    db().jobs.update_many({"deleted": True, "cache_retained": True, "media_expired": True,
                           "artifact": {"$exists": False}}, [{"$project": {
        "owner": 1, "request_id": 1, "request_keys": 1, "state": 1, "day": 1, "slot": 1,
        "extra_ticket": 1, "skip_ticket": 1, "minute_grants": 1, "minute_units": 1, "deleted": 1}}])


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
            if uses_postgres():
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
    if store_history.configured():
        tasks.append(asyncio.create_task(store_history.run()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="ReelDub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


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
    return """ReelDub Cloud privacy

ReelDub uses your Apple sign-in identifier and the email Apple shares to manage your account. Hosted account and job records are held in Neon Postgres. Video and poster bytes stay in our private media cache.

When uploads are enabled, ReelDub stores private source files temporarily, normally for 24 hours, and while confirmed work needs them. ReelDub fetches submitted Reel URLs from Instagram and processes the audio and video on its render workers. Cloudflare routes hosted requests. Your account can access your job history, reports and videos. The service administrator can review submitted Reel links, titles, language choices, issue reports, account contact information and processing statistics for support and moderation. A minimal suspension record may be retained to enforce a ban. Sharing a video exports a copy to the destination you choose. After the app confirms a download, the server keeps the video for 24 hours from the last eligible request. Undelivered videos expire after seven days. Removing a dub hides it from your library, revokes playback links and clears its issue report. A private cached copy can be reused by your account until its deadline. Saved dubs remain on your device until removed.

Apple processes App Store payments. ReelDub stores verified transaction identifiers, products, subscription expiration or revocation, and credit usage to deliver purchases across devices. ReelDub does not receive your payment card details. Job deletion preserves the minimum purchase and quota records needed to prevent duplicate spending.

Deleting your Cloud account erases your profile, sessions, dubs and reports. A short-lived deletion marker prevents reuse during the daily quota period. Minimal pseudonymous purchase and spent-credit records allow the same Apple account to recover its balance. Active Apple subscriptions continue until canceled through Apple. Other devices and destinations may retain downloaded or exported copies.

A self-hosted server is controlled by its operator. Its storage, network and retention settings may differ.
"""


@app.get("/v1/me")
def me(user=Depends(restricted_owner), checkout_version: str = Header(default="", alias="X-ReelDub-Checkout")):
    with db().profile_reads() if uses_postgres() else nullcontext():
        account = db().accounts.find_one({"_id": user}, {"email": 1, "library_id": 1, "tier": 1, "store_token": 1, "banned": 1}) or {}
        if account.get("banned"):
            return {"tier":"suspended","used":0,"limit":0,"purchase_preflight":False,
                    "library_id":account.get("library_id",hash_value(user)),"email":account.get("email")}
        result = quota(user, tier(user, account), now())
        result["admin_console"] = console.permitted(user)
        if result["admin_console"]:
            result["lab_runtimes"] = console.runtimes()
        result["output_languages"] = outputs(os.getenv("REELDUB_OUTPUT_PROFILE", "light"))
        result["library_id"] = account.get("library_id", hash_value(user))
        result["email"] = account.get("email")
        result.update(store.balances(user))
        result["creator_minutes"] = minutes.enabled()
        result["batch_progress"] = True
        if minutes.enabled():
            monthly, pack = minutes.funding(user,result["tier"],now())
            result.update(monthly_seconds=len(monthly), pack_seconds=len(pack))
        store_connected = checkout_version == "1" and uses_postgres() and store.enabled()
        result["purchase_preflight"] = store_connected and minutes.enabled() and user.startswith("apple:")
    if store_connected and user.startswith("apple:"):
        result["store_token"] = account.get("store_token") or store.store_token(user)
    return result


class ReelSource(BaseModel):
    url: str = Field(max_length=2048)


class QuoteRequest(BaseModel):
    source_id: str = Field(min_length=24, max_length=24)
    targets: list[str] = Field(min_length=1, max_length=11)
    trim: bool = False
    model_config = {"extra":"forbid"}


def creator_access(user):
    if not minutes.enabled():
        raise HTTPException(404,"Creator minutes are unavailable on this server.")


@app.post("/v1/sources/reel")
def stage_reel(body: ReelSource, user=Depends(owner)):
    creator_access(user)
    limit_writes(user,"intake",12)
    return sources.reel(user,canonical_url(body.url))


@app.post("/v1/sources/upload")
async def stage_upload(request: Request, user=Depends(owner)):
    await run_in_threadpool(creator_access,user)
    await run_in_threadpool(limit_writes,user,"intake",12)
    if request.headers.get("content-type","").split(";")[0] not in ("video/mp4","video/quicktime","application/octet-stream"):
        raise HTTPException(415,"Choose an MP4 or MOV file.")
    title=unquote(request.headers.get("x-video-title",""))[:140].strip() or "Imported video"
    with sources.admission(user) as stage, TemporaryFile() as file:
        digest=hashlib.sha256()
        async for part in request.stream():
            if file.tell()+len(part)>sources.MAX_BYTES:
                raise HTTPException(413,"Choose a file under 80 MiB.")
            digest.update(part)
            file.write(part)
        return await run_in_threadpool(sources.upload,stage,user,file,digest.hexdigest(),title)


@app.get("/v1/sources/{source_id}")
def source_info(source_id: str, user=Depends(owner)):
    creator_access(user)
    return sources.public(sources.get(user, source_id))


@app.post("/v1/quotes")
def create_quote(body: QuoteRequest,user=Depends(owner)):
    creator_access(user)
    limit_writes(user,"quote",60)
    targets=list(dict.fromkeys(body.targets))
    if len(targets)!=len(body.targets) or any(t not in outputs(os.getenv("REELDUB_OUTPUT_PROFILE","light")) for t in targets):
        raise HTTPException(422,"Select supported output languages once each.")
    return minutes.quote(user,body.source_id,targets,body.trim)


@app.post("/v1/quotes/{quote_id}/confirm")
def confirm_quote(quote_id: UUID,user=Depends(owner)):
    creator_access(user)
    limit_writes(user,"confirm",60)
    try:
        return {"jobs": minutes.confirm(user,str(quote_id)), "remaining": [], "detail": None}
    except minutes.PartialConfirmation as partial:
        return {"jobs": partial.jobs, "remaining": partial.remaining, "detail": partial.detail}


class PurchaseIntent(BaseModel):
    product: str = Field(min_length=1, max_length=128)


@app.post("/v1/store/purchase-intents")
def prepare_purchase(body: PurchaseIntent, user=Depends(owner)):
    if not user.startswith("apple:") or os.getenv("REELDUB_ACCESS_MODE") != "hosted":
        raise HTTPException(403, "Sign in with Apple to make a purchase.")
    limit_writes(user, "checkout", 6)
    return checkout.prepare(user, body.product)


class SignedTransaction(BaseModel):
    signed_transaction: str = Field(min_length=1, max_length=24000)
    intent: UUID | None = None


@app.post("/v1/store/transactions", status_code=204)
def purchase(body: SignedTransaction, user=Depends(owner)):
    limit_writes(user, "purchase", 30)
    if not user.startswith("apple:"):
        raise HTTPException(403, "Use your ReelDub Cloud account for purchases.")
    transaction = store.verify(body.signed_transaction)
    key = store.record(transaction, user)
    if body.intent and key:
        db().purchase_intents.update_one(
            {"_id":str(body.intent), "owner":user, "product":transaction.productId,
             "environment":transaction.rawEnvironment, "$or":[{"transaction":None},{"transaction":key}]},
            {"$set":{"transaction":key}})



class AppleNotification(BaseModel):
    signedPayload: str = Field(min_length=1, max_length=48000)


@app.post("/v1/store/notifications", status_code=204)
def notification(body: AppleNotification):
    store.handle_notification(body.signedPayload)


@app.post("/v1/apple/challenge")
def challenge(request: Request):
    if os.getenv("REELDUB_ACCESS_MODE", "hosted") != "hosted" or not os.getenv("APPLE_CLIENT_ID"):
        raise HTTPException(503, "This server uses a connection key. Apple sign-in requires a configured app ID.")
    stamp = now()
    client_ip = request.client.host
    if os.getenv("TRUST_TUNNEL_IP") == "1":
        client_ip = request.headers.get("CF-Connecting-IP") or client_ip
    ip = hash_value(client_ip)
    challenges = media_db().signin_challenges if uses_postgres() else db().challenges
    if challenges.count_documents({"ip": ip, "expires": {"$gt": stamp}}, limit=20) >= 20:
        raise HTTPException(429, "Try signing in again later.")
    value = ("v1." if uses_postgres() else "") + secrets.token_urlsafe(32)
    challenges.insert_one({"_id": hash_value(value), "ip": ip, "expires": stamp + timedelta(minutes=5)})
    return {"challenge": value}


class AppleLogin(BaseModel):
    identity_token: str = Field(max_length=12000)
    challenge: str = Field(min_length=32, max_length=100)


def verify_identity(login: AppleLogin):
    audience = os.getenv("APPLE_CLIENT_ID")
    if os.getenv("REELDUB_ACCESS_MODE", "hosted") != "hosted" or not audience:
        raise HTTPException(503, "Apple sign-in is not configured.")
    nonce = hash_value(login.challenge)
    challenge_query = {"_id": nonce, "expires": {"$gt": now()}}
    local_nonce = login.challenge.startswith("v1.")
    if local_nonce and not media_db().signin_challenges.find_one(challenge_query, {"_id": 1}):
        raise HTTPException(401, "The sign-in challenge expired. Try again.")
    try:
        key = apple_keys.get_signing_key_from_jwt(login.identity_token).key
        claims = jwt.decode(login.identity_token, key, algorithms=["RS256"], audience=audience,
                            issuer="https://appleid.apple.com", options={"require": ["exp", "iat", "sub", "nonce"]})
    except jwt.PyJWTError:
        raise HTTPException(401, "Apple could not verify this sign-in.")
    if claims["nonce"] != nonce:
        raise HTTPException(401, "The sign-in challenge expired.")
    # Older builds can finish a sign-in started just before rollout. Only a verified
    # Apple identity reaches the legacy challenge store.
    challenges = media_db().signin_challenges if local_nonce else db().challenges
    if not challenges.find_one_and_delete({"_id": nonce, "expires": {"$gt": now()}}):
        raise HTTPException(401, "The sign-in challenge expired.")
    return claims


@app.post("/v1/apple/session")
def apple(login: AppleLogin):
    claims = verify_identity(login)
    user = "apple:" + claims["sub"]
    if erasure.closing(user):
        raise HTTPException(409, "This account was deleted. New registration opens after the daily quota resets and deletion finishes.")
    if db().accounts.find_one({"_id":user,"banned":True},{"_id":1}):
        # Keep Apple-verified erasure available without repopulating a moderation tombstone.
        token = secrets.token_urlsafe(48)
        db().sessions.insert_one({"_id":hash_value(token),"owner":user,"expires":now()+timedelta(minutes=15)})
        return {"token":token}
    admin_email = os.getenv("ADMIN_APPLE_EMAIL", "").strip().casefold()
    verified = claims.get("email_verified") in (True, "true")
    email = claims.get("email")
    update = {"$set": {"last_sign_in": now()}, "$setOnInsert": {"created": now(), "library_id": str(uuid4())}}
    if verified and isinstance(email, str):
        update["$set"]["email"] = email
        db().beta_allowances.update_one(
            {"_id": beta_invite_id(email), "owner": {"$exists": False}},
            {"$set": {"owner": user, "claimed": now()}},
        )
    if admin_email and verified and isinstance(email, str) and email.casefold() == admin_email:
        update["$set"]["tier"] = "admin"
    db().accounts.update_one({"_id": user}, update, upsert=True)
    erasure.restore_billing(user)
    token = secrets.token_urlsafe(48)
    db().sessions.insert_one({"_id": hash_value(token), "owner": user,
                              "expires": now() + timedelta(days=30)})
    return {"token": token}


class DeleteAccount(AppleLogin):
    authorization_code: str = Field(default="", max_length=4096)


@app.delete("/v1/account")
def delete_account(body: DeleteAccount, user=Depends(restricted_owner)):
    if not user.startswith("apple:") or "apple:" + verify_identity(body)["sub"] != user:
        raise HTTPException(403, "Confirm deletion with the Apple Account that owns these dubs.")
    db().account_deletions.update_one({"_id": user}, {"$setOnInsert": {"created": now()}}, upsert=True)
    erasure.erase(user)
    return {"apple_revoked": erasure.revoke_apple(body.authorization_code, user, apple_keys)}


@app.delete("/v1/session", status_code=204)
def logout(authorization: str = Header(), user=Depends(restricted_owner)):
    # The review key is a reusable, operator-provisioned credential until expiry or revocation.
    if user != BETA_REVIEWER:
        db().sessions.delete_one({"_id": hash_value(authorization.split(" ", 1)[1]), "owner": user})
    db().playback.delete_many({"owner": user})


class Submission(BaseModel):
    url: str = Field(max_length=2048)
    request_id: UUID
    target_language: str = Field(default="en", min_length=2, max_length=2)

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


def next_position():
    counter = db().counters.find_one_and_update({"_id": "queue"}, {"$inc": {"seq": 1}},
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
        before = db().jobs.count_documents({"state": "queued", "$or": [
            {"priority": {"$gt": job["priority"]}}, {"priority": job["priority"], **earlier}]})
    return dict(id=job["_id"], url=job["url"], state=job["state"], stage=job["stage"], created=job["created"],
                progress=job.get("progress"), estimated_finish=job.get("estimated_finish") if job["state"]=="running" else None,
                last_requested=job.get("last_requested"), started=job.get("started"), finished=job.get("finished"), ahead=before, lane="premium" if job.get("priority") else "free", error=job.get("error"),
                attempt=job["attempt"], reported=bool(job.get("report")), media_seconds=job.get("media_seconds"),
                title=job.get("title"), author=job.get("author"), has_poster=job.get("has_poster", False),
                available_on_server=media_available(job), cache_expires=job.get("media_expires"),
                source_id=str(job["source_file"]) if job.get("source_file") else None, trimmed=job.get("trimmed",False),
                render_quality=job.get("render_quality"), lab_mode=job.get("lab_mode"), lab_runtime=job.get("lab_runtime"), voice_batch=job.get("voice_batch"), target_language=job.get("target_language", "en"), source_language=job.get("source_language"), first_caption=job.get("first_caption"))


def enqueue_render(job, plan, limit):
    """Admit new work only with server-allocated funding. Unique keys reserve it at insert."""
    if job["day"] != now().date().isoformat():
        raise HTTPException(409, "Your daily allowance reset. Review this dub again.")
    if plan != "admin":
        slot = job.get("slot")
        daily = type(slot) is int and 0 <= slot < limit
        if minutes.enabled():
            seconds = job.get("quoted_seconds")
            if not job.get("source_file") or type(seconds) is not int or not 1 <= seconds <= 600:
                raise HTTPException(428, "Review the duration and cost before starting a dub.")
            units = job.get("minute_units", [])
            funded = (daily and seconds <= 60 and not units) or (
                slot is None and len(set(units)) == len(units) == seconds
            )
        else:
            funded = daily or bool(job.get("extra_ticket"))
        if not funded:
            raise HTTPException(402, "There is not enough time in your balance.")
    ticket = notify(preparing=True)
    if not ticket:
        raise HTTPException(503, "The render queue is temporarily unavailable. Try again shortly.")
    if uses_postgres():
        if not db().admit_job(job):
            raise HTTPException(403,"This account is suspended.")
    else:
        db().jobs.insert_one(job)
        if job["owner"].startswith("apple:") and db().accounts.find_one({"_id":job["owner"],"banned":True},{"_id":1}):
            console.cancel_pending(job["owner"])
            raise HTTPException(403,"This account is suspended.")
    notify(ticket=ticket)


@app.post("/v1/jobs")
def submit(body: Submission, user=Depends(owner)):
    limit_writes(user, "submit", 90)
    url = canonical_url(body.url)
    target = body.target_language
    quality = default_render_quality()
    if target not in outputs(os.getenv("REELDUB_OUTPUT_PROFILE", "light")):
        raise HTTPException(422, "This server does not support the selected output language.")
    target_filter = {"$in": ["en", None]} if target == "en" else target
    key = {"owner": user, "request_id": str(body.request_id)}
    request_id = key["request_id"]
    def prior_request():
        previous = db().jobs.find_one({"owner": user, "$or": [
            {"request_id": request_id}, {"request_keys": request_id}]}, {"poster": 0})
        if previous:
            if previous.get("deleted"):
                raise HTTPException(410, "This dub was deleted.")
            if previous.get("lab_mode") or previous["url"] != url or previous.get("target_language", "en") != target:
                raise HTTPException(409, "This request was already used for another Reel.")
            return summary(previous)

    if previous := prior_request():
        return previous
    plan = tier(user)
    stamp = now()
    try:
        cached = db().jobs.find_one_and_update(
            {"owner": user, "url": url, "lab_mode":{"$exists":False}, "render_quality": quality, "trimmed":{"$ne":True}, "target_language": target_filter, **live_media_query(stamp, include_cache=True),
             "$and": [{"$or": [{"deleted": {"$ne": True}}, {"cache_retained": True}]}]},
            [{"$unset": ["deleted", "cache_retained"]}, {"$set": {"last_requested": stamp, "cache_hits": cache_hit_count(request_id), "media_expires": renew_media(stamp), "request_keys": {
                "$setUnion": [{"$ifNull": ["$request_keys", []]}, [request_id]]}}}],
            projection={"poster": 0}, sort=[("created", -1)], return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:
        if previous := prior_request():
            return previous
        raise
    if cached:
        return summary(cached)
    if minutes.enabled() and plan != "admin":
        raise HTTPException(428,"Review the duration and cost in the latest ReelDub app before starting a new dub.")
    limit = daily_limit(user, plan)
    if limit is not None:
        failed = db().jobs.count_documents({"owner": user, "day": stamp.date().isoformat(), "state": "failed"},
                                           limit=limit * 2)
        if failed >= limit * 2:
            raise HTTPException(429, "Too many failed renders today. Try again after midnight UTC.")
    job = dict(_id=str(uuid4()), **key, request_keys=[request_id], url=url, target_language=target, state="queued", stage="Queued", created=stamp,
               priority=int(plan != "free"), queue_seq=next_position(), day=stamp.date().isoformat(), attempt=0)
    if quality:
        job["render_quality"] = quality
    # The quota slot and job share one document. Unique indexes serialize concurrent submissions.
    occupied = set(db().jobs.distinct("slot", {"owner": user, "day": job["day"]})) if limit is not None else set()
    for slot in range(limit or 1):
        if slot in occupied:
            continue
        if limit is not None:
            job["slot"] = slot
        try:
            enqueue_render(job, plan, limit)
            return summary(job)
        except DuplicateKeyError:
            if previous := prior_request():
                return previous
    job.pop("slot", None)
    for ticket in store.tickets(user, "extra_ticket"):
        try:
            enqueue_render({**job, "extra_ticket": ticket}, plan, limit)
            return summary(job)
        except DuplicateKeyError:
            if previous := prior_request():
                return previous
    if previous := prior_request():
        return previous
    raise HTTPException(429, "Your daily dubs are used up. They reset at midnight UTC.",
                        headers={"X-ReelDub-Reason": "daily_allowance_used"})


@app.post("/v1/jobs/{job_id}/skip")
def skip(job_id: str, user=Depends(owner)):
    limit_writes(user, "skip", 30)
    job = owned(job_id, user)
    if job.get("priority"):
        return summary(job)
    if job["state"] != "queued":
        raise HTTPException(409, "This dub has already left the queue.")
    if not db().jobs.find_one({"state": "queued", "priority": 0, **(
            {"$or": [{"queue_seq": None}, {"queue_seq": {"$lt": job["queue_seq"]}}]} if "queue_seq" in job else
            {"queue_seq": None, "$or": [{"created": {"$lt": job["created"]}},
              {"created": job["created"], "_id": {"$lt": job["_id"]}}]})}, {"_id": 1}):
        raise HTTPException(409, "You are already first in the standard line. Your skip was saved.")
    for ticket in store.tickets(user, "skip_ticket"):
        try:
            promoted = db().jobs.find_one_and_update(
                {"_id": job_id, "owner": user, "state": "queued", "priority": 0},
                {"$set": {"priority": 1, "queue_seq": next_position(), "skip_ticket": ticket}},
                projection={"poster": 0}, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            continue
        if promoted:
            return summary(promoted)
        break
    job = owned(job_id, user)
    if job.get("priority"):
        return summary(job)
    if job["state"] != "queued":
        raise HTTPException(409, "This dub has already left the queue.")
    raise HTTPException(409, "No queue skips available.")


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


@app.delete("/v1/jobs/{job_id}", status_code=204)
def delete_dub(job_id: str, user=Depends(owner)):
    key = {"_id": job_id, "owner": user}
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
        {**key, "deleted": {"$ne": True}, "state": {"$in": ["done", "failed"]},
         "$or": [{"artifact": {"$exists": False}}, {"media_expired": True}, {"media_expires": {"$lte": stamp}}]},
        [{"$project": {"owner": 1, "request_id": 1, "request_keys": 1, "state": 1, "day": 1, "slot": 1, "extra_ticket": 1, "skip_ticket": 1, "minute_grants": 1, "minute_units": 1,
                       "artifact": 1, "deleted": {"$literal": True}}}],
        return_document=ReturnDocument.AFTER)
    if not job:
        owned(job_id, user)
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
        raise HTTPException(404, "This Reel has no poster.")
    return Response(bytes(image), media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@app.post("/v1/jobs/{job_id}/playback")
def playback(job_id: str, user=Depends(owner)):
    job = requested_media(job_id, user)
    ticket = secrets.token_urlsafe(36)
    db().playback.insert_one({"_id": hash_value(ticket), "job": job_id, "owner": user,
                              "generation": job.get("play_generation", 0), "expires": now() + timedelta(minutes=30)})
    return {"ticket": ticket}


@app.api_route("/v1/play/{job_id}/{ticket}", methods=["GET", "HEAD"])
def stream(job_id: str, ticket: str, request: Request, range: str | None = Header(default=None)):
    if uses_postgres():
        grant = db().playback_grant(hash_value(ticket), job_id, now())
    else:
        grant = db().playback.find_one({"_id": hash_value(ticket), "job": job_id, "expires": {"$gt": now()}})
        if grant and db().accounts.find_one({"_id":grant["owner"],"banned":True},{"_id":1}):
            grant = None
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


app.include_router(console.router(owner))
