"""Creator quotes and reservations. A job reserves its funding in one MongoDB insert."""

import os
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from . import sources, store
from .db import db, now


def enabled():
    return os.getenv("REELDUB_CREATOR_MINUTES", "0") == "1"


def funding(user, plan, stamp):
    """Disjoint Apple-period and permanent pack units. Failed jobs return units atomically."""
    env = os.getenv("STORE_ENVIRONMENT") or "Production"
    grants = db().purchases.find(
        {"owner": user, "environment": env, "revoked": False, "product": store.MINUTE_PACK}, {"seconds": 1}
    )
    packs = [(grant["_id"], grant["seconds"]) for grant in grants]
    period = store.active_period(user, [store.CREATOR])
    if period:
        monthly = [(period["_id"], 7200)]
    elif plan == "paid" and (
        db().accounts.find_one({"_id": user, "tier": "paid"}) or store.legacy_premium(user)
    ):
        monthly = [(f"manual/{sha256(user.encode()).hexdigest()}/{stamp:%Y-%m}", 7200)]
    else:
        monthly = []
    grant_ids = [key for key, _ in monthly + packs]
    spent = set()
    if grant_ids:
        for job in db().jobs.find({"owner": user, "minute_grants": {"$in": grant_ids}}, {"minute_units": 1}):
            spent.update(job.get("minute_units", []))

    def available(grants):
        for key, count in grants:
            prefix = key + "#"
            used = {unit for unit in spent if unit.startswith(prefix)}
            remaining = count - len(used)
            for i in range(count):
                if remaining <= 0:
                    break
                unit = prefix + str(i)
                if unit not in used:
                    remaining -= 1
                    yield unit

    return list(available(monthly)), list(available(sorted(packs)))


def existing(user, url, target, trim):
    from .api import live_media_query

    key = {
        "owner": user,
        "url": url,
        "target_language": {"$in": [target, None]} if target == "en" else target,
        "lab_mode": {"$exists":False},
        "trimmed": True if trim else {"$ne": True},
    }
    return db().jobs.find_one(
        {
            **key,
            "$or": [
                {"state": {"$in": ["queued", "running"]}, "deleted": {"$ne": True}},
                {
                    **live_media_query(now(), include_cache=True),
                    "$and": [{"$or": [{"deleted": {"$ne": True}}, {"cache_retained": True}]}],
                },
            ],
        },
        {"poster": 0},
        sort=[("created", -1)],
    )


def quote(user, source_id, targets, trim):
    from .api import daily_limit, tier

    source = sources.get(user, source_id)
    stamp = now()
    plan = tier(user)
    trimmed = trim and source["seconds"] > 60
    duration = 60 if trimmed else source["seconds"]
    day = stamp.date().isoformat()
    occupied = set(db().jobs.distinct("slot", {"owner": user, "day": day}))
    slots = sum(i not in occupied for i in range(daily_limit(user, plan) or 0))
    monthly, pack = funding(user, plan, stamp) if plan != "admin" else ([], [])
    m, p = len(monthly), len(pack)
    lines = []
    for target in targets:
        job = existing(user, source["identity"], target, trimmed)
        line = {
            "target": target,
            "free": 0,
            "monthly": 0,
            "pack": 0,
            "missing": 0,
            "reused": bool(job),
            "unlimited": False,
            "request": str(uuid4()),
        }
        if not job:
            if plan == "admin":
                line["unlimited"] = True
            elif duration <= 60 and slots:
                line["free"] = 1
                slots -= 1
            else:
                line["monthly"] = min(duration, m)
                m -= line["monthly"]
                line["pack"] = min(duration - line["monthly"], p)
                p -= line["pack"]
                line["missing"] = duration - line["monthly"] - line["pack"]
        lines.append(line)
    record = {
        "_id": str(uuid4()),
        "owner": user,
        "source": source["_id"],
        "seconds": duration,
        "trimmed": trimmed,
        "lines": lines,
        "day": day,
        "expires": stamp + timedelta(minutes=15),
    }
    db().quotes.insert_one(record)
    return {
        "id": record["_id"],
        "seconds": duration,
        "lines": lines,
        "monthly_left": m,
        "pack_left": p,
        "free_left": slots,
    }


class PartialConfirmation(Exception):
    def __init__(self, jobs, remaining, detail):
        self.jobs, self.remaining, self.detail = jobs, remaining, detail


def confirm(user, quote_id):
    from .api import cache_hit_count, daily_limit, enqueue_render, next_position, renew_media, summary, tier

    record = db().quotes.find_one({"_id": quote_id, "owner": user})
    if not record:
        raise HTTPException(404, "Quote unavailable. Review the cost again.")

    def prior(line):
        return db().jobs.find_one({"owner": user, "request_keys": line["request"]}, {"poster": 0})

    def response(job):
        if job.get("deleted"):
            raise HTTPException(410, "This confirmed dub was deleted.")
        return summary(job)

    previous = [prior(line) for line in record["lines"]]
    if all(previous):
        return [response(job) for job in previous]
    if record["expires"] <= now() or record["day"] != now().date().isoformat():
        raise HTTPException(409, "This quote expired. Review the cost again.")
    if any(x["missing"] for x in record["lines"]):
        raise HTTPException(402, "There is not enough time in your balance.")
    source = sources.get(user, str(record["source"]))
    plan = tier(user)

    limit = daily_limit(user, plan) or 0

    def confirm_line(line):
        old = prior(line)
        if old:
            return response(old)
        target = line["target"]
        for attempt in range(8):
            stamp = now()
            cached = existing(user, source["identity"], target, record["trimmed"])
            if cached:
                from .api import live_media_query

                active = {"state": {"$in": ["queued", "running"]}, "deleted": {"$ne": True}}
                live = {
                    **live_media_query(stamp, include_cache=True),
                    "$and": [{"$or": [{"deleted": {"$ne": True}}, {"cache_retained": True}]}],
                }
                reuse = db().jobs.find_one_and_update(
                    {"_id": cached["_id"], "$or": [active, live]},
                    [
                        {"$unset": ["deleted", "cache_retained"]},
                        {
                            "$set": {
                                "last_requested": stamp,
                                "cache_hits": cache_hit_count(line["request"]),
                                "media_expires": renew_media(stamp),
                                "request_keys": {
                                    "$setUnion": [{"$ifNull": ["$request_keys", []]}, [line["request"]]]
                                },
                            }
                        },
                    ],
                    projection={"poster": 0},
                    return_document=ReturnDocument.AFTER,
                )
                if reuse:
                    return response(reuse)
                continue
            if line["reused"] or line["unlimited"] and plan != "admin":
                raise HTTPException(409, "The quote changed. Review the cost again.")
            identity = sha256(
                repr((user, source["identity"], target, record["trimmed"])).encode()
            ).hexdigest()
            job = {
                "_id": line["request"],
                "owner": user,
                "request_id": line["request"],
                "request_keys": [line["request"]],
                "render_key": identity,
                "source_file": source["_id"],
                "url": source["identity"],
                "title": source["title"],
                "author": source.get("author"),
                "trimmed": record["trimmed"],
                "quoted_seconds": record["seconds"],
                "target_language": target,
                "state": "queued",
                "stage": "Queued",
                "created": stamp,
                "attempt": 0,
                "priority": int(plan != "free" or line["pack"] > 0),
                "queue_seq": next_position(),
                "day": record["day"],
            }
            if line["free"]:
                used = db().jobs.distinct("slot", {"owner": user, "day": record["day"]})
                slot = next((i for i in range(limit) if i not in used), None)
                if slot is None:
                    raise HTTPException(409, "The daily allowance changed. Review the cost again.")
                job["slot"] = slot
            if line["monthly"] or line["pack"]:
                if (
                    record["seconds"] <= 60
                    and len(db().jobs.distinct("slot", {"owner": user, "day": record["day"]})) < limit
                ):
                    raise HTTPException(409, "A free daily dub is available. Review the lower cost.")
                monthly, pack = funding(user, plan, stamp)
                if len(monthly) < line["monthly"] or len(pack) < line["pack"]:
                    raise HTTPException(409, "Your balance changed. Review the cost again.")
                job["minute_units"] = monthly[: line["monthly"]] + pack[: line["pack"]]
                job["minute_grants"] = sorted({u.rsplit("#", 1)[0] for u in job["minute_units"]})
            try:
                enqueue_render(job, plan, limit)
                return response(job)
            except DuplicateKeyError:
                old = prior(line)
                if old:
                    return response(old)
                # A completed copy may have expired before the periodic sweep removes its deduplication key.
                db().jobs.update_many(
                    {"render_key": identity, "state": "done", "media_expires": {"$lte": stamp}},
                    {"$unset": {"render_key": ""}},
                )
        raise HTTPException(409, "Another submission changed the balance. Review the quote again.")

    result = []
    for pos, line in enumerate(record["lines"]):
        try:
            result.append(confirm_line(line))
        except HTTPException as exc:
            if not result:
                raise
            raise PartialConfirmation(
                result, [x["target"] for x in record["lines"][pos:]], exc.detail
            ) from exc
    return result
