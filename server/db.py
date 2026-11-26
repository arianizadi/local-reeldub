import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from pymongo import MongoClient
from pymongo.write_concern import WriteConcern


QUEUE_ORDER = [("recovery_rank", -1), ("priority", -1), ("queue_seq", 1), ("created", 1), ("_id", 1)]


def now():
    return datetime.now(timezone.utc)


@lru_cache
def media_db():
    ca = os.getenv("MONGO_TLS_CA_FILE")
    options = {"tls": True, "tlsCAFile": ca} if ca else {}
    client = MongoClient(os.environ["MONGO_URI"], tz_aware=True, serverSelectionTimeoutMS=6000, **options)
    return client.get_database(os.environ.get("MONGO_DATABASE", "reeldub"))


def uses_postgres():
    return bool(os.getenv("REELDUB_POSTGRES_DSN") or os.getenv("REELDUB_POSTGRES_DSN_FILE"))


def uses_mongo_metadata():
    return bool(os.getenv("REELDUB_METADATA_MONGO_URI_FILE"))


def separate_metadata():
    return uses_postgres() or uses_mongo_metadata()


@lru_cache
def metadata_mongo():
    uri = Path(os.environ["REELDUB_METADATA_MONGO_URI_FILE"]).read_text().strip()
    database = os.environ["REELDUB_METADATA_MONGO_DATABASE"]
    if database in {"admin", "local", "config"}:
        raise ValueError("ReelDub requires a dedicated application database")
    options = {}
    if os.getenv("REELDUB_TEST_DATABASE") != "1":
        options.update(tls=True, tlsAllowInvalidCertificates=False, tlsAllowInvalidHostnames=False)
        if ca := os.getenv("REELDUB_METADATA_MONGO_CA_FILE"):
            options["tlsCAFile"] = ca
    client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=6000,
                         connectTimeoutMS=6000, timeoutMS=15000, **options)
    return client.get_database(database, write_concern=WriteConcern(w="majority", j=True, wtimeout=10000))


@lru_cache
def db():
    if uses_mongo_metadata():
        if uses_postgres():
            raise RuntimeError("Configure exactly one authoritative ReelDub metadata store")
        return metadata_mongo()
    if not uses_postgres():
        return media_db()
    from .postgres import Postgres
    file = os.getenv("REELDUB_POSTGRES_DSN_FILE")
    dsn = Path(file).read_text().strip() if file else os.environ["REELDUB_POSTGRES_DSN"]
    return Postgres(dsn)


def setup_indexes():
    if separate_metadata():
        db().command("ping")  # Schema is provisioned separately with the migration role.
        if uses_postgres():
            return
    mongo_indexes(db())


def mongo_indexes(store):
    store.jobs.create_index([("owner", 1), ("request_id", 1)], unique=True)
    store.jobs.create_index([("state", 1), ("priority", -1), ("created", 1)])
    store.jobs.create_index([("owner", 1), ("request_keys", 1)], unique=True,
                            partialFilterExpression={"request_keys": {"$exists": True}})
    store.jobs.create_index([("owner", 1), ("url", 1), ("created", -1)])
    store.jobs.create_index([("owner", 1), ("url", 1), ("target_language", 1), ("created", -1)])
    store.jobs.create_index([("source_file",1),("state",1)],sparse=True)
    store.jobs.create_index("media_expires", sparse=True)
    store.jobs.create_index([("state", 1), *QUEUE_ORDER])
    store.sources.create_index([("owner",1),("slot",1)], unique=True, partialFilterExpression={"slot":{"$exists":True}})
    store.sources.create_index([("owner",1),("identity",1)])
    store.sources.create_index("expires")
    for name in ("playback", "request_rates"):
        store[name].create_index("expires", expireAfterSeconds=0)
    store.fleet_claim_locks.create_index("expires", expireAfterSeconds=0)
    store.jobs.create_index("fleet_slot", unique=True, name="one_running_job_per_slot",
                            partialFilterExpression={"fleet_slot": {"$type": "string"}, "state": "running"})
    store.jobs.create_index([("fleet_slot", 1), ("fleet_request", 1)], unique=True,
                            partialFilterExpression={"fleet_slot": {"$type": "string"}, "fleet_request": {"$type": "string"}})
