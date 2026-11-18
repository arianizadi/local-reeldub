import os
from datetime import datetime, timezone
from functools import lru_cache

from pymongo import MongoClient


def now():
    return datetime.now(timezone.utc)


@lru_cache
def db():
    client = MongoClient(os.environ["MONGO_URI"], tz_aware=True, serverSelectionTimeoutMS=6000)
    return client.get_database(os.environ.get("MONGO_DATABASE", "reeldub"))


def setup_indexes():
    store = db()
    store.jobs.create_index([("owner", 1), ("request_id", 1)], unique=True)
    store.jobs.create_index([("owner", 1), ("day", 1), ("slot", 1)], unique=True,
                            partialFilterExpression={"slot": {"$exists": True}})
    store.jobs.create_index([("state", 1), ("priority", -1), ("created", 1)])
    for name in ("sessions", "challenges"):
        store[name].create_index("expires", expireAfterSeconds=0)

