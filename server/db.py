import os
from datetime import datetime, timezone
from functools import lru_cache

from pymongo import MongoClient


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


@lru_cache
def db():
    if not uses_postgres():
        return media_db()
    from pathlib import Path
    from .postgres import Postgres
    file = os.getenv("REELDUB_POSTGRES_DSN_FILE")
    dsn = Path(file).read_text().strip() if file else os.environ["REELDUB_POSTGRES_DSN"]
    return Postgres(dsn)


def setup_indexes():
    if uses_postgres():
        db().command("ping")  # Schema is provisioned separately with the migration role.
        if os.getenv("REELDUB_ACCESS_MODE") == "hosted":
            # Short-lived pre-login nonces never need to wake the account database.
            challenges = media_db().signin_challenges
            challenges.create_index("expires", expireAfterSeconds=0)
            challenges.create_index([("ip", 1), ("expires", 1)])
        return
    store = db()
    store.jobs.create_index([("owner", 1), ("request_id", 1)], unique=True)
    store.jobs.create_index([("owner", 1), ("day", 1), ("slot", 1)], unique=True,
                            partialFilterExpression={"slot": {"$exists": True}})
    store.jobs.create_index([("state", 1), ("priority", -1), ("created", 1)])
    store.jobs.create_index([("owner", 1), ("request_keys", 1)], unique=True,
                            partialFilterExpression={"request_keys": {"$exists": True}})
    store.jobs.create_index([("owner", 1), ("url", 1), ("created", -1)])
    store.jobs.create_index([("owner", 1), ("url", 1), ("target_language", 1), ("created", -1)])
    store.jobs.create_index([("source_file",1),("state",1)],sparse=True)
    store.jobs.create_index("media_expires", sparse=True)
    store.jobs.create_index([("state", 1), *QUEUE_ORDER])
    for field in ("extra_ticket", "skip_ticket"):
        store.jobs.create_index(field, unique=True, partialFilterExpression={field: {"$type": "string"}})
    store.jobs.create_index([("owner",1),("minute_grants",1)])
    store.purchases.create_index([("environment",1),("original",1),("product",1)])
    store.subscription_status.create_index("purchase")
    store.jobs.create_index("minute_units", unique=True, partialFilterExpression={"minute_units": {"$exists": True}})
    store.jobs.create_index("render_key", unique=True, sparse=True)
    store.sources.create_index([("owner",1),("slot",1)], unique=True, partialFilterExpression={"slot":{"$exists":True}})
    store.sources.create_index([("owner",1),("identity",1)])
    store.sources.create_index("expires")
    store.quotes.create_index("expires", expireAfterSeconds=86400)
    store.purchases.create_index([("owner", 1), ("revoked", 1)])
    store.beta_allowances.create_index("owner", sparse=True)
    store.accounts.create_index("store_token", unique=True, sparse=True)
    store.billing_bindings.create_index("token", unique=True)
    for name in ("sessions", "challenges", "playback", "request_rates", "account_deletions"):
        store[name].create_index("expires", expireAfterSeconds=0)
