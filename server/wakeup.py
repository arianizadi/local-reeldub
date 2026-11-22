"""Hints in the private media store let idle GPU workers leave Neon suspended."""

import logging
import os
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from pymongo.errors import PyMongoError

from .db import db, media_db, now, uses_postgres

logger = logging.getLogger(__name__)

def next_hour(stamp):
    return stamp.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def notify(*, preparing=False, ticket=None):
    generation = uuid4().hex
    if uses_postgres():
        try:
            fields = {"generation": generation}
            if preparing:
                stamp = now()
                # Only an uncontended admission may cancel the recovery window.
                # Overlapping producers, including older API versions, retain it.
                fields["preparing"] = {"$cond": [
                    {"$lte": [{"$ifNull": ["$check_until", stamp]}, stamp]}, generation, None]}
                fields["check_until"] = {"$max": ["$check_until", stamp + timedelta(minutes=2)]}
            elif ticket:
                fields["check_until"] = {"$cond": [
                    {"$and": [{"$eq": ["$preparing", ticket]}, {"$eq": ["$generation", ticket]}]},
                    "$$REMOVE", "$check_until"]}
            media_db().queue_wake.update_one({"_id": "gpu"}, [{"$set": fields}], upsert=True)
        except PyMongoError as error:
            logger.warning("GPU wake hint unavailable (%s)", type(error).__name__)
            return False
    if path := os.getenv("REELDUB_WAKE_FILE"):
        try:
            Path(path).touch(mode=0o600)
        except OSError as error:
            logger.warning("Local worker signal unavailable (%s)", type(error).__name__)
    return generation


class Wake:
    def __init__(self):
        self.generation = None
        self.next_scan = 0
        self.last_scan = float("-inf")
        self.hints_down = False
        self.settle_until = None
        self.watcher = None
        if path := os.getenv("REELDUB_WAKE_FILE"):
            try:
                from inotify_simple import INotify, flags
                Path(path).touch(mode=0o600)
                watcher = INotify()
                watcher.add_watch(path, flags.ATTRIB | flags.MODIFY)
                self.watcher = watcher
            except (ImportError, OSError) as error:
                logger.warning("Using timed queue checks (%s)", type(error).__name__)

    def wait(self):
        # Every worker has its own watch. A signal queued between claim() and
        # read() wakes all workers; the database still decides who owns a job.
        delay = 60 if self.watcher else 5
        if self.hints_down:
            delay = min(delay, 15)
        elif uses_postgres():
            delay = min(delay, max(0, self.next_scan - time.monotonic()))
            if self.settle_until:
                delay = min(delay, 5, max(0, (self.settle_until - now()).total_seconds()))
        if self.watcher:
            try:
                from inotify_simple import flags
                events = self.watcher.read(timeout=round(delay * 1000))
                if not any(event.mask & flags.IGNORED for event in events):
                    return
            except OSError as error:
                logger.warning("Lost local worker signal (%s)", type(error).__name__)
            self.watcher.close()
            self.watcher = None
            return
        time.sleep(delay)

    def watch_active_leases(self):
        if uses_postgres():
            active = db().jobs.find_one(
                {"state": "running", "deleted": {"$ne": True}, "lease_end": {"$gt": now()}},
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
        final_check = self.settle_until is not None and stamp >= self.settle_until
        self.settle_until = expiry if expiry > stamp else None
        settling = self.settle_until is not None and tick - self.last_scan >= 5
        if not self.hints_down and not final_check and not settling and tick < self.next_scan and self.generation == generation:
            return False
        self.hints_down = False
        self.generation = generation
        self.last_scan = tick
        self.next_scan = tick + (next_hour(stamp) - stamp).total_seconds()
        return True
