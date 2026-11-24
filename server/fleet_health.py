"""Node health and recovery. Idle probes and the watchdog election live in the private media database."""
from datetime import timedelta
from functools import lru_cache
import json
import os
from pathlib import Path
import re

import gridfs
from pymongo import ReturnDocument
from pymongo.errors import AutoReconnect, DuplicateKeyError

from .db import db, media_db, now
from .wakeup import notify
from .worker import MAX_ATTEMPTS
from .upload_voice import KITTEN_ENGINE, KITTEN_MATCH


SUSPECT = timedelta(seconds=35)
OFFLINE = timedelta(seconds=80)
PREFLIGHT_AGE = timedelta(minutes=20)
PROBE_CONFIRM = timedelta(seconds=30)
FAULT_WINDOW = timedelta(minutes=10)
MAX_ATTEMPT_AGE = timedelta(hours=1)
REASONS = ("gpu_unavailable", "model_unavailable", "runtime_unavailable")


@lru_cache
def tickets():
    table = media_db().fleet_requests
    table.create_index("expires", expireAfterSeconds=0)
    table.create_index([("watch", 1), ("due", 1)])
    table.create_index([("job", 1), ("lease", 1)], sparse=True)
    return table


def registry():
    config = os.environ["REELDUB_FLEET_REGISTRY"]
    return json.loads(config if config.lstrip().startswith("{") else Path(config).read_text())["nodes"]


@lru_cache(maxsize=1)
def sync_registry(nodes):
    store = media_db().fleet_nodes
    for name, slots, enabled, studio in nodes:
        store.update_one({"_id": name}, {"$set": {"slots": slots, "enabled": enabled, "studio": studio,
                                                  "configured": True}}, upsert=True)
    store.update_many({"configured": True, "_id": {"$nin": [n[0] for n in nodes]}},
                      {"$set": {"enabled": False}})


def status(doc, stamp, *, enabled=True):
    if not doc or doc.get("offline_marked") or doc.get("last_seen", stamp - OFFLINE) <= stamp - OFFLINE:
        return "offline"
    if doc.get("reason") or doc.get("blocked_until", stamp) > stamp:
        return "quarantined"
    if not enabled or not doc.get("enabled", True):
        return "draining"
    if (not doc.get("ready") or doc["last_seen"] <= stamp - SUSPECT
            or doc.get("last_preflight", stamp - PREFLIGHT_AGE) <= stamp - PREFLIGHT_AGE):
        return "suspect"
    return "online"


def health(node, ready, preflight, reason, kitten_narrator=False, kitten_match=False):
    table, stamp = media_db().fleet_nodes, now()
    key = {"_id": node.name}
    observed = {"last_seen": stamp, "slots": node.slots, "enabled": node.enabled, "studio": node.studio,
                "kitten_narrator": bool(node.studio and kitten_narrator), "kitten_match": bool(node.studio and kitten_match)}
    old = table.find_one_and_update(key, {"$set": observed, "$setOnInsert": {"ready": False}}, upsert=True)
    if old and old.get("last_seen", stamp - OFFLINE) <= stamp - OFFLINE:
        # A late keepalive cannot silently restore a node that was offline.
        table.update_one({**key, "last_seen": stamp}, {"$set": {"ready": False},
                         "$unset": {"probe_failed_at": "", "last_preflight": ""}})
    if preflight and not ready:
        # Scratch and CUDA headroom are admission checks. A single failure suspends
        # new claims while existing valid jobs may still renew and finish.
        failed = old and old.get("probe_failed_at")
        if failed and failed <= stamp - PROBE_CONFIRM:
            confirmed = table.update_one({**key, "probe_failed_at": failed, "reason": None},
                                         {"$set": {"ready": False, "reason": reason or "runtime_unavailable"},
                                          "$unset": {"probe_failed_at": ""}})
            if confirmed.matched_count:
                fault(node.name, reason or "runtime_unavailable")
        else:
            table.update_one({**key, "probe_failed_at": None, "reason": None},
                             {"$set": {"ready": False, "probe_failed_at": stamp}})
    elif preflight and ready:
        table.update_one({**key, "$or": [{"blocked_until": {"$exists": False}}, {"blocked_until": {"$lte": stamp}}]},
                         {"$set": {"ready": True, "last_preflight": stamp}, "$unset": {
                             "probe_failed_at": "", "offline_marked": "", "reason": "", "blocked_until": ""}})
    doc = table.find_one(key)
    state = status(doc, stamp, enabled=node.enabled)
    return {"status": state, "needs_preflight": bool(node.enabled and state != "online" and
                                                     doc.get("blocked_until", stamp) <= stamp)}


def fault(node_name, reason):
    stamp = now()
    table = media_db().fleet_nodes
    row = table.find_one_and_update({"_id": node_name}, [
        {"$set": {"faults": {"$cond": [{"$gte": ["$last_fault", stamp - FAULT_WINDOW]},
                                       {"$add": [{"$ifNull": ["$faults", 0]}, 1]}, 1]},
                  "ready": False, "reason": reason, "last_fault": stamp}},
        {"$set": {"blocked_until": {"$cond": [{"$gte": ["$faults", 2]}, stamp + timedelta(seconds=120),
                                              stamp + timedelta(seconds=60)]}}}
    ], upsert=True, return_document=ReturnDocument.AFTER)
    tickets().update_many({"node": node_name, "watch": True}, {"$min": {"due": stamp}})
    return row


def admin_nodes():
    stamp = now()
    return [{"node": doc["_id"], "status": status(doc, stamp), "last_seen": doc.get("last_seen"),
             "last_preflight": doc.get("last_preflight"), "slots": doc.get("slots", 0), "enabled": doc.get("enabled", True),
             "reason": doc.get("reason"), "faults": doc.get("faults", 0), "recoveries": doc.get("recoveries", 0)}
            for doc in media_db().fleet_nodes.find({}).sort("_id", 1)]


def idle_alternative_engines(node):
    """Engines available on at least one compatible, currently idle alternate GPU."""
    stamp = now()
    query = {"_id": {"$ne": node.name}, "enabled": True, "ready": True,
             "last_seen": {"$gt": stamp - SUSPECT}, "last_preflight": {"$gt": stamp - PREFLIGHT_AGE}}
    if node.studio:
        query["studio"] = True
    candidates = list(media_db().fleet_nodes.find(query, {"slots": 1, "kitten_narrator": 1, "kitten_match": 1}))
    if not candidates:
        return set()
    names = [row["_id"] for row in candidates]
    busy = set(tickets().distinct("slot", {"node": {"$in": names}, "watch": True}))
    engines = set()
    for row in candidates:
        if any(f"{row['_id']}:{slot}" not in busy for slot in range(row["slots"])):
            engines.add(None)  # The existing Studio/Light compatibility filter applies to ordinary jobs.
            if row.get("kitten_narrator"):
                engines.add(KITTEN_ENGINE)
            if row.get("kitten_match"):
                engines.add(KITTEN_MATCH)
    return engines


def recover(job, reason, *, only_expired=False):
    stamp = now()
    node = job["fleet_slot"].rsplit(":", 1)[0]
    gate = {"_id": job["_id"], "lease": job["lease"], "fleet_slot": job["fleet_slot"], "state": "running",
            "deleted": {"$ne": True}}
    if only_expired:
        gate["lease_end"] = {"$lte": stamp}
    elif reason == "node_offline":
        gate["lease_end"] = job["lease_end"]
    release = ["lease", "lease_end", "fleet_slot", "fleet_request", "upload_lease", "upload_until", "upload_token"]
    fields = {"recovery_reason": reason, "recovery_from_node": node, "estimated_finish": None}
    if job.get("attempt", 0) >= MAX_ATTEMPTS:
        fields.update(state="failed", stage="Failed", finished=stamp, error="The render server could not complete this dub after repeated interruptions.")
        release += ["slot", "minute_grants", "minute_units", "render_key", "extra_ticket", "skip_ticket"]
    else:
        fields.update(state="queued", stage="Recovering render", progress=0, recovery_rank=1)
    ticket = notify(preparing=True)
    if not ticket:
        raise AutoReconnect("Queue recovery signal unavailable")
    updated = db().jobs.update_one(gate, {"$set": fields, "$inc": {"recovery_count": 1},
                                         "$unset": dict.fromkeys(release, "")})
    if updated.matched_count:
        try:
            tickets().update_one({"_id": job["fleet_slot"] + "/" + job["fleet_request"]},
                                 {"$set": {"watch": False}})
            media_db().fleet_nodes.update_one({"_id": node}, {"$inc": {"recoveries": 1}})
        finally:
            notify(ticket=ticket)
        return True
    notify(ticket=ticket)
    return False


def watch(job):
    node = job["fleet_slot"].rsplit(":", 1)[0]
    query = {"_id": job["fleet_slot"] + "/" + job["fleet_request"]}
    tickets().update_one(query, {"$set": {"node": node, "slot": job["fleet_slot"], "request": job["fleet_request"],
                                "job": job["_id"], "lease": job["lease"], "watch": True, "due": job["lease_end"],
                                "expires": now() + timedelta(days=1)}}, upsert=True)


def bootstrap():
    # After loss of local media state, rebuild the small active assignment index once.
    marker = media_db().maintenance
    if marker.find_one({"_id": "fleet-assignment-bootstrap"}):
        return
    pending = db().jobs.find({"state": "running", "fleet_slot": {"$ne": None}, "fleet_request": {"$ne": None},
                              "lease": {"$ne": None}, "deleted": {"$ne": True}})
    for job in pending:
        watch(job)
    marker.update_one({"_id": "fleet-assignment-bootstrap"}, {"$set": {"finished": now()}}, upsert=True)


def watchdog():
    table, stamp = media_db().maintenance, now()
    try:
        table.update_one({"_id": "fleet-watchdog", "$or": [{"due": {"$lte": stamp}}, {"due": {"$exists": False}}]},
                         {"$set": {"due": stamp + timedelta(seconds=5)}}, upsert=True)
    except DuplicateKeyError:
        return
    sync_registry(tuple(sorted((name, r["slots"], r.get("enabled", True), r.get("studio", False)) for name, r in registry().items()
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) and type(r.get("slots")) is int
                    and 1 <= r["slots"] <= 32 and type(r.get("enabled", True)) is bool and type(r.get("studio", False)) is bool)))
    bootstrap()
    index = tickets()
    nodes = media_db().fleet_nodes
    for silent in nodes.find({"last_seen": {"$lte": stamp - OFFLINE}, "offline_marked": {"$ne": True}}):
        marked = nodes.update_one({"_id": silent["_id"], "last_seen": silent["last_seen"]},
                                  {"$set": {"offline_marked": True, "ready": False, "reason": "node_offline"}})
        if marked.matched_count:
            index.update_many({"node": silent["_id"], "watch": True}, {"$min": {"due": stamp}})
    unhealthy = [n["_id"] for n in nodes.find({"$or": [{"offline_marked": True},
                 {"ready": False, "reason": {"$in": list(REASONS)}}]}, {"_id": 1})]
    needs_check = {"watch": True, "$or": [{"due": {"$lte": stamp}},
                   {"node": {"$in": unhealthy}, "job": {"$exists": True}}]}
    for ticket in index.find(needs_check).sort("due", 1).limit(100):
        scope = {"fleet_slot": ticket["slot"], "fleet_request": ticket["request"], "state": "running",
                 "deleted": {"$ne": True}}
        if ticket.get("job"):
            scope.update(_id=ticket["job"], lease=ticket["lease"])
        job = db().jobs.find_one(scope)
        if not job:
            index.update_one({"_id": ticket["_id"]}, {"$set": {"watch": False}})
            continue
        node = media_db().fleet_nodes.find_one({"_id": ticket["node"]})
        state = status(node, stamp)
        if node and node.get("offline_marked") and state == "offline":
            recover(job, "node_offline")
        elif state == "quarantined" and node and node.get("reason") in REASONS:
            recover(job, node["reason"])
        elif job["lease_end"] <= stamp:
            recover(job, "node_offline" if state == "offline" else "lease_expired", only_expired=True)
        else:
            watch(job)
    # A coordinator can exit after writing GridFS chunks and before creating its files header.
    # These intent IDs let us reclaim both kinds of orphan after checking the authoritative result.
    uploads = media_db().fleet_uploads
    for upload in uploads.find({"due": {"$lte": stamp}}).limit(25):
        job = db().jobs.find_one({"_id": upload["job"]}, {"artifact": 1, "lease_end": 1, "upload_token": 1})
        if job and job.get("artifact") == upload["_id"]:
            uploads.delete_one({"_id": upload["_id"]})
        elif job and job.get("upload_token") == upload["token"] and job.get("lease_end", stamp) > stamp:
            uploads.update_one({"_id": upload["_id"]}, {"$set": {"due": stamp + timedelta(minutes=5)}})
        else:
            gridfs.GridFS(media_db()).delete(upload["_id"])
            uploads.delete_one({"_id": upload["_id"]})
