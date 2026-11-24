"""Free allowance and queue measurements from admitted jobs. No periodic sampling."""

import os
from datetime import timedelta
from math import floor

from .db import db, separate_metadata, uses_postgres
from .languages import outputs
from .fleet_health import registry
from .worker import TEMPORARY_DB_ERRORS

WINDOW_DAYS = 30
# Compact library tombstones retain the observation. Full account erasure removes it.
TOMBSTONE_FIELDS = {
    "launch_free_slot": 1, "launch_priority_at": 1, "launch_busy": 1,
    "started": {"$cond": [{"$ifNull": ["$launch_priority_at", False]}, {"$ifNull": ["$started", None]}, None]},
}


def priority_entry(job, stamp):
    fields = {"launch_priority_at": stamp}
    config = os.getenv("REELDUB_FLEET_REGISTRY")
    if not config or not separate_metadata():
        return fields
    # An unavailable or malformed configuration leaves occupancy unknown. Render
    # leases are scheduling observations, not GPU utilization measurements.
    try:
        nodes = registry()
        needs_studio = bool(job.get("render_quality") or job.get("lab_mode")
                            or job.get("target_language", "en") not in outputs("light"))
        slots = set()
        for name, node in nodes.items():
            if type(node["slots"]) is not int or not 1 <= node["slots"] <= 32:
                return fields
            if node.get("enabled", True) and (not needs_studio or node.get("studio", False)):
                slots.update(f"{name}:{slot}" for slot in range(node["slots"]))
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return fields
    if slots:
        try:
            leases = db().jobs.distinct("fleet_slot", {"state": "running", "deleted": {"$ne": True},
                                       "lease_end": {"$gt": stamp}, "fleet_slot": {"$in": sorted(slots)}})
        except TEMPORARY_DB_ERRORS:
            return fields  # A missed observation must not prevent admission.
        fields["launch_busy"] = slots.issubset(leases)
    return fields


def overview(stamp):
    start = (stamp - timedelta(days=WINDOW_DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if uses_postgres():
        counts = db().launch_summary(start, stamp)
    else:
        day = {"$gte": start.date().isoformat(), "$lte": stamp.date().isoformat()}
        active_days = set()
        full_days = set()
        for job in db().jobs.find({"day": day, "launch_free_slot": {"$exists": True}},
                                  {"owner": 1, "day": 1, "launch_free_slot": 1}):
            key = (job["owner"], job["day"])
            active_days.add(key)
            if job["launch_free_slot"] == 4:
                full_days.add(key)
        waits, busy = [], []
        known = 0
        scope = {"launch_priority_at": {"$gte": start, "$lte": stamp}, "started": {"$gte": start, "$lte": stamp}}
        for job in db().jobs.find(scope, {"started": 1, "launch_priority_at": 1, "launch_busy": 1}):
            elapsed = (job["started"] - job["launch_priority_at"]).total_seconds()
            if elapsed < 0:
                continue
            waits.append(elapsed)
            known += isinstance(job.get("launch_busy"), bool)
            if job.get("launch_busy"):
                busy.append(elapsed)

        def percentiles(samples):
            samples.sort()
            def at(q):
                if not samples:
                    return None
                index = q * (len(samples) - 1)
                lo = floor(index)
                return samples[lo] + (index - lo) * (samples[min(lo + 1, len(samples) - 1)] - samples[lo])
            return len(samples), at(.5), at(.95)

        counts = (len({u for u, _ in active_days}), len({u for u, _ in full_days}),
                  len(active_days), len(full_days), *percentiles(waits), *percentiles(busy), known)
    keys = ("free_active_users", "free_limit_users", "free_active_user_days", "free_limit_user_days",
            "priority_started", "priority_median_seconds", "priority_p95_seconds",
            "busy_priority_started", "busy_priority_median_seconds", "busy_priority_p95_seconds",
            "priority_occupancy_known")
    return {"window_days": WINDOW_DAYS, **dict(zip(keys, counts, strict=True))}
