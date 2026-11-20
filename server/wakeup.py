"""Hints in the private media store let idle GPU workers leave Neon suspended."""

import logging
import time
from datetime import timedelta
from uuid import uuid4

from pymongo.errors import PyMongoError

from .db import db, media_db, now, uses_postgres

logger = logging.getLogger(__name__)

def next_hour(stamp):
    return stamp.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def notify(*, preparing=False):
    if uses_postgres():
        try:
            update = {"$set": {"generation": uuid4().hex}}
            if preparing:
                # A producer can stop after the SQL commit, before its second notification.
                update["$max"] = {"check_until": now() + timedelta(minutes=2)}
            media_db().queue_wake.update_one(
                {"_id": "gpu"}, update, upsert=True
            )
        except PyMongoError as error:
            logger.warning("GPU wake hint unavailable (%s)", type(error).__name__)
            return False
    return True


class Wake:
    def __init__(self):
        self.generation = None
        self.next_scan = 0
        self.last_scan = float("-inf")
        self.hints_down = False
        self.settle_until = None

    def watch_active_leases(self):
        if uses_postgres():
            active = db().jobs.find_one(
                {"state": "running", "lease_end": {"$exists": True}},
                {"lease_end": 1},
                sort=[("lease_end", 1)],
            )
            if active:
                until = max(2, (active["lease_end"] - now()).total_seconds() + 1)
                self.next_scan = min(self.next_scan, time.monotonic() + until)

    def due(self):
        if not uses_postgres():
            return True
        try:
            hint = media_db().queue_wake.find_one({"_id": "gpu"}) or {}
        except PyMongoError:
            self.hints_down = True
            if time.monotonic() - self.last_scan < 15:
                return False
            self.last_scan = time.monotonic()
            return True
        tick, stamp = time.monotonic(), now()
        generation = hint.get("generation")
        expiry = hint.get("check_until", stamp)
        if expiry > stamp:
            self.settle_until = expiry
        final_check = self.settle_until is not None and stamp >= self.settle_until
        settling = self.settle_until is not None and tick - self.last_scan >= 5
        if not self.hints_down and not final_check and not settling and tick < self.next_scan and self.generation == generation:
            return False
        if final_check:
            self.settle_until = None
        self.hints_down = False
        self.generation = generation
        self.last_scan = tick
        self.next_scan = tick + (next_hour(stamp) - stamp).total_seconds()
        return True
