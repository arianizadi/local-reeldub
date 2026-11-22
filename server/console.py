"""On-demand operator views. All entry points require the pinned Apple identity."""

import os
import secrets
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError, PyMongoError

from .db import db, media_db, now, uses_postgres
from .source import reel_url
from .languages import LANGUAGES, outputs


def runtimes():
    enabled = set(os.getenv("REELDUB_LAB_RUNTIMES", "").split(","))
    return ["standard"] + [x for x in ("graph", "omni") if x in enabled]


def permitted(user):
    subject = os.getenv("ADMIN_APPLE_SUB")
    return bool(
        subject
        and os.getenv("REELDUB_ACCESS_MODE") == "hosted"
        and user == "apple:" + subject
    )


class Input(BaseModel):
    model_config = {"extra": "forbid"}


class Page(Input):
    reports_only: bool = False
    before: datetime | None = None
    after_id: str = Field(default="", max_length=260)
    account: str | None = Field(default=None, max_length=260)


class Moderation(Input):
    account: str = Field(min_length=7, max_length=260)
    ban: bool
    reason: str = Field(default="", max_length=240)


class Experiment(Input):
    url: str = Field(max_length=2048)
    request_id: UUID
    target: str = Field(default="en", min_length=2, max_length=2)
    mode: Literal["best", "medium", "fast"]
    runtime: Literal["standard", "graph", "omni"] = "standard"
    batch: Literal[1, 2] = 2
    separation: Literal["fp32", "fp16"] = "fp32"
    voice: Literal["default", "matched", "small", "preset"] = "default"


JOB_FIELDS = {
    key: 1
    for key in (
        "owner",
        "url",
        "title",
        "state",
        "stage",
        "created",
        "started",
        "finished",
        "source_language",
        "target_language",
        "media_seconds",
        "quoted_seconds",
        "error",
        "report",
        "cache_hits",
        "lab_mode",
        "render_quality",
        "voice_batch",
        "lab_separation",
        "asr_model",
        "voice_model",
        "translation_model",
        "lab_runtime",
        "lab_voice",
        "priority",
        "attempt",
        "progress",
        "last_requested",
        "media_expires",
    )
}


def job_row(row):
    try:
        link = reel_url(row.get("url") or "")
    except ValueError:
        link = None
    return {
        "id": row["_id"],
        "account": row["owner"],
        "url": link,
        **{k: row.get(k) for k in JOB_FIELDS if k not in ("owner", "url", "report")},
        "report": {k: row["report"].get(k) for k in ("category", "note", "status")}
        if row.get("report")
        else None,
    }


def gpu_snapshot():
    if os.getenv("REELDUB_LOCAL_GPU_METRICS") != "1":
        return None
    fields = [
        "name",
        "utilization.gpu",
        "memory.used",
        "memory.total",
        "temperature.gpu",
        "power.draw",
    ]
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "-i",
                os.getenv("REELDUB_DEVICE_ID", "0"),
                "--query-gpu=" + ",".join(fields),
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        name, utilization, used, total, temperature, power = (
            result.stdout.strip().split(", ")
        )
        return {
            "name": name[:100],
            "load": int(utilization),
            "used_mib": int(used),
            "total_mib": int(total),
            "temperature": int(temperature),
            "watts": float(power) if power.replace(".", "", 1).isdigit() else None,
            "sampled": now(),
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def metrics():
    cutoff = now() - timedelta(days=30)
    if uses_postgres():
        accounts, banned, signed_in = db().admin_accounts(cutoff)
        groups = db().admin_performance()
    else:
        accounts = db().accounts.count_documents({})
        banned = db().accounts.count_documents({"banned": True})
        signed_in = db().accounts.count_documents({"last_sign_in": {"$gte": cutoff}})
        pipeline = [
            {"$match": {"deleted": {"$ne": True}}},
            {
                "$group": {
                    "_id": {
                        "state": "$state",
                        "source": "$source_language",
                        "target": {"$ifNull": ["$target_language", "en"]},
                        "mode": {"$ifNull": ["$lab_mode", {"$ifNull": ["$render_quality", "standard"]}]},
                    },
                    "count": {"$sum": 1},
                    "hits": {"$sum": "$cache_hits"},
                    "reported": {"$sum": {"$cond": ["$report", 1, 0]}},
                    "runtime": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$state", "done"]},
                                {
                                    "$divide": [
                                        {"$subtract": ["$finished", "$started"]},
                                        1000,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "seconds": {
                        "$sum": {
                            "$cond": [{"$eq": ["$state", "done"]}, "$media_seconds", 0]
                        }
                    },
                    "wait": {
                        "$sum": {
                            "$divide": [{"$subtract": ["$started", "$created"]}, 1000]
                        }
                    },
                    "started": {"$sum": {"$cond": ["$started", 1, 0]}},
                }
            },
        ]
        groups = [
            (
                d["_id"].get("state"),
                d["_id"].get("source"),
                d["_id"]["target"],
                d["_id"]["mode"],
                d["count"],
                d["hits"],
                d["reported"],
                d["runtime"],
                d["seconds"],
                d["wait"],
                d["started"],
            )
            for d in db().jobs.aggregate(pipeline)
        ]
    states, pairs, speeds = Counter(), Counter(), {}
    hits = reports = starts = 0
    wait = 0.0
    for (
        state,
        src,
        dst,
        mode,
        count,
        reused,
        reported,
        runtime,
        duration,
        queued,
        started,
    ) in groups:
        states[state] += count
        hits += reused
        reports += reported
        starts += started
        wait += float(queued)
        pairs[(src or "unknown", dst)] += count
        if state == "done":
            row = speeds.setdefault(mode, [0, 0.0, 0.0])
            row[0] += count
            row[1] += float(runtime)
            row[2] += float(duration)
    fleet, nodes = [], []
    if uses_postgres() and os.getenv("MONGO_URI"):
        try:
            rows = media_db().fleet_telemetry.find({"sampled": {"$gt": now() - timedelta(minutes=3)}}).sort("sampled", -1)
            fleet = [{"node": row.pop("_id"), **row} for row in rows]
            from .fleet_health import admin_nodes
            nodes = admin_nodes()
        except PyMongoError:
            pass
    return {
        "states": dict(states),
        "gpu": (fleet[0] if fleet else gpu_snapshot()),
        "fleet": fleet,
        "nodes": nodes,
        "accounts": accounts,
        "banned": banned,
        "signed_in_30d": signed_in,
        "cache_hits": hits,
        "reports": reports,
        "avg_wait": wait / starts if starts else None,
        "languages": [
            {"source": s, "target": t, "count": n} for (s, t), n in pairs.most_common()
        ],
        "speeds": [
            {
                "mode": m,
                "count": n,
                "average": r / n,
                "per_minute": 60 * r / d if d else None,
            }
            for m, (n, r, d) in sorted(speeds.items())
        ],
    }


def cancel_pending(target):
    # A running worker loses its fence. Unique quota and credit reservations are released on failure.
    db().jobs.update_many(
        {"owner": target, "state": {"$in": ["queued", "running"]}},
        {
            "$set": {
                "state": "failed",
                "stage": "Failed",
                "error": "Account suspended.",
                "finished": now(),
            },
            "$unset": {
                k: ""
                for k in (
                    "slot",
                    "extra_ticket",
                    "skip_ticket",
                    "minute_units",
                    "minute_grants",
                    "render_key",
                    "lease",
                    "lease_end",
                )
            },
        },
    )


def router(owner):
    from .api import (
        AppleLogin,
        canonical_url,
        enqueue_render,
        hash_value,
        limit_writes,
        next_position,
        quota,
        summary,
        tier,
        verify_identity,
    )
    from . import minutes

    def administrator(response: Response, user=Depends(owner)):
        response.headers["Cache-Control"] = "private, no-store"
        if not permitted(user):
            raise HTTPException(403, "Administrator access required.")
        return user

    root = APIRouter(prefix="/v1/admin", dependencies=[Depends(administrator)])

    @root.post("/unlock")
    def unlock(
        proof: AppleLogin, authorization: str = Header(), user=Depends(administrator)
    ):
        limit_writes(user, "console-unlock", 6)
        claims = verify_identity(proof)
        if "apple:" + claims["sub"] != user:
            raise HTTPException(403, "Use the Apple account that owns the console.")
        token = secrets.token_urlsafe(40)
        store = media_db().signin_challenges if uses_postgres() else db().challenges
        store.insert_one(
            {
                "_id": "admin/" + hash_value(token),
                "bound": hash_value(authorization),
                "expires": now() + timedelta(minutes=15),
            }
        )
        return {"token": token}

    def fresh(
        authorization: str = Header(),
        grant: str = Header(default="", alias="X-ReelDub-Admin"),
    ):
        store = media_db().signin_challenges if uses_postgres() else db().challenges
        if not 30 <= len(grant) <= 100 or not store.find_one(
            {
                "_id": "admin/" + hash_value(grant),
                "bound": hash_value(authorization),
                "expires": {"$gt": now()},
            },
            {"_id": 1},
        ):
            raise HTTPException(
                403,
                "Verify with Apple to unlock the admin console.",
                headers={"X-ReelDub-Reason": "admin_verification_required"},
            )

    routes = APIRouter(dependencies=[Depends(fresh)])

    cache_lock = threading.Lock()
    cached = None
    expires = 0.0

    @routes.get("/overview")
    def overview():
        nonlocal cached, expires
        with cache_lock:
            if time.monotonic() >= expires or cached is None:
                cached = metrics()
                expires = time.monotonic() + 60
            return cached

    @routes.post("/activity")
    def activity(page: Page):
        query = {"deleted": {"$ne": True}}
        if page.reports_only:
            query["report"] = {"$exists": True}
        if page.account:
            query["owner"] = page.account
        if page.before or page.after_id:
            if not page.before or not page.before.tzinfo or not page.after_id:
                raise HTTPException(422, "Invalid activity cursor.")
            query["$or"] = [
                {"created": {"$lt": page.before}},
                {"created": page.before, "_id": {"$lt": page.after_id}},
            ]
        rows = list(
            db()
            .jobs.find(query, JOB_FIELDS)
            .sort([("created", -1), ("_id", -1)])
            .limit(41)
        )
        return {"rows": [job_row(j) for j in rows[:40]], "more": len(rows) > 40}

    @routes.post("/accounts")
    def accounts(page: Page):
        query = {"_id": {"$gt": page.after_id}} if page.after_id else {}
        rows = list(
            db()
            .accounts.find(
                query,
                {
                    "email": 1,
                    "tier": 1,
                    "banned": 1,
                    "moderated_at": 1,
                    "moderation_reason": 1,
                    "moderation_history": 1,
                    "last_sign_in": 1,
                    "created": 1,
                },
            )
            .sort("_id", 1)
            .limit(41)
        )
        return {
            "rows": [
                {"protected": permitted(row["_id"]), "id": row.pop("_id"), **row}
                for row in rows[:40]
            ],
            "more": len(rows) > 40,
        }

    class AccountSelection(Input):
        account: str = Field(min_length=7, max_length=260)

    @routes.post("/allowance")
    def allowance(body: AccountSelection):
        account = db().accounts.find_one(
            {"_id": body.account}, {"tier": 1, "banned": 1}
        )
        if not account:
            raise HTTPException(404, "Account unavailable.")
        stamp = now()
        plan = tier(body.account, account)
        result = quota(body.account, plan, stamp)
        if minutes.enabled() and plan != "admin":
            monthly, packs = minutes.funding(body.account, plan, stamp)
            result.update(monthly_seconds=len(monthly), pack_seconds=len(packs))
        return result

    @routes.post("/moderation")
    def moderate(action: Moderation, user=Depends(administrator)):
        target = action.account
        if (
            not target.startswith("apple:")
            or permitted(target)
            or (action.ban and not action.reason.strip())
        ):
            raise HTTPException(
                422, "Select another Apple account and give a reason for a ban."
            )
        limit_writes(user, "moderation", 20)
        erased = {
            "_id": target,
            "banned": True,
            "$or": [
                {"erased": True},
                # Records erased before the explicit flag retained just the subject, ban and timestamp.
                {
                    "moderated_at": {"$exists": True},
                    **{
                        k: {"$exists": False}
                        for k in (
                            "created",
                            "email",
                            "last_sign_in",
                            "tier",
                            "library_id",
                            "moderation_history",
                            "store_token",
                        )
                    },
                },
            ],
        }
        if not action.ban and db().accounts.delete_one(erased).deleted_count:
            return {"banned": False}
        if action.ban and db().accounts.find_one(erased, {"_id": 1}):
            return {"banned": True}
        # Both the state and the reason commit together. Ban checks apply even to pre-existing sessions.
        stamp = now()
        note = action.reason.strip()
        event = {"ban": action.ban, "at": stamp, "reason": note, "by": user}
        changed = db().accounts.update_one(
            {"_id": target, "erased": {"$ne": True}},
            [
                {
                    "$set": {
                        "banned": action.ban,
                        "moderated_at": stamp,
                        "moderated_by": user,
                        "moderation_reason": {"$literal": note},
                        "moderation_history": {
                            "$slice": [
                                {
                                    "$concatArrays": [
                                        {"$ifNull": ["$moderation_history", []]},
                                        {"$literal": [event]},
                                    ]
                                },
                                -20,
                            ]
                        },
                    }
                }
            ],
        )
        if not changed.matched_count:
            raise HTTPException(404, "Account unavailable for moderation.")
        if action.ban:
            cancel_pending(target)
            db().playback.delete_many({"owner": target})
        return {"banned": action.ban}

    @routes.post("/experiments")
    def experiment(body: Experiment, user=Depends(administrator)):
        if os.getenv(
            "REELDUB_OUTPUT_PROFILE", "light"
        ) != "studio" or body.target not in outputs("studio"):
            raise HTTPException(
                422, "A Studio worker and a supported language are required."
            )
        url = canonical_url(body.url)
        if body.runtime not in runtimes() or (
            body.runtime != "standard"
            and (
                body.voice != "matched" or (body.runtime == "graph" and body.batch != 1)
            )
        ):
            raise HTTPException(
                422,
                "Select an installed runtime with a compatible matched voice and batch size.",
            )
        output = LANGUAGES[body.target]
        if (body.voice == "preset" and not output.voices) or (
            body.voice in ("matched", "small") and not output.matched
        ):
            raise HTTPException(
                422, "This voice is unavailable for the selected language."
            )
        key = {"owner": user, "request_id": str(body.request_id)}
        options = {
            "url": url,
            "lab_mode": body.mode,
            "target_language": body.target,
            "voice_batch": body.batch,
            "lab_voice": body.voice,
            "lab_runtime": body.runtime,
            "lab_separation": body.separation,
        }

        def existing():
            row = db().jobs.find_one(
                {
                    "owner": user,
                    "$or": [
                        {"request_id": str(body.request_id)},
                        {"request_keys": str(body.request_id)},
                    ],
                },
                {"poster": 0},
            )
            if row and (
                row.get("deleted")
                or any(
                    row.get(k, "fp32" if k == "lab_separation" else None) != v
                    for k, v in options.items()
                )
            ):
                raise HTTPException(409, "This experiment ID has already been used.")
            return row

        row = existing()
        if row is None:
            limit_writes(user, "experiment", 12)
            stamp = now()
            row = {
                "_id": str(body.request_id),
                **key,
                **options,
                "request_keys": [str(body.request_id)],
                "state": "queued",
                "stage": "Queued",
                "created": stamp,
                "day": stamp.date().isoformat(),
                "priority": 1,
                "queue_seq": next_position(),
                "attempt": 0,
            }
            try:
                enqueue_render(row, "admin", None)
            except DuplicateKeyError:
                row = existing()
                if row is None:
                    raise HTTPException(409, "Choose a new experiment ID.")
        return summary(row)

    root.include_router(routes)
    return root
