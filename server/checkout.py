"""Admit new checkouts only after a durable ledger write with spare storage."""

import logging
import os
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException
from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout
from pymongo.errors import DuplicateKeyError

from . import minutes, store
from .db import db, now, uses_postgres
from .postgres import encode

logger = logging.getLogger(__name__)
UNAVAILABLE = "Purchases are temporarily paused. No payment was started."


def prepare(user, product):
    if (
        not minutes.enabled()
        or not uses_postgres()
        or not store.enabled()
        or product not in (store.CREATOR, store.MINUTE_PACK)
    ):
        raise HTTPException(503, UNAVAILABLE)
    return commit_intent(user, product, os.environ["STORE_ENVIRONMENT"])


def commit_intent(user, product, environment):
    try:
        intent = str(uuid4())
        token = intent if product == store.SELF_HOST else store.store_token(user)
        limit = int(os.environ["REELDUB_STORE_STORAGE_LIMIT_BYTES"])
        if limit <= 0:
            raise ValueError("Missing storage budget")
        stamp = now()
        record = {
            "_id": intent,
            "owner": user,
            "product": product,
            "account": token,
            "environment": environment,
            "created": stamp,
            "expires": stamp + timedelta(days=30),
        }
        with db().connection() as conn:
            conn.execute("SET LOCAL synchronous_commit = on")
            # One dedicated project/branch. Include other accessible databases on this endpoint.
            size, provider_limit = conn.execute("""
                SELECT sum(pg_database_size(oid)), pg_size_bytes(current_setting('neon.max_cluster_size',true))
                FROM pg_database WHERE has_database_privilege(oid,'CONNECT')
                """).fetchone()
            if provider_limit and provider_limit > 0:
                limit = min(limit, provider_limit)
            if size >= limit * 4 // 5:
                logger.warning("Checkout paused at database storage watermark")
                raise HTTPException(503, UNAVAILABLE)
            # Proves we can commit to the authoritative store. Payment deliveries never use this gate.
            conn.execute(
                "INSERT INTO reeldub.purchase_intents(doc) VALUES (%s)",
                (Jsonb(encode(record)),),
            )
        return {"id": record["_id"], "account": token, "product": product}
    except (
        DatabaseError,
        PoolTimeout,
        DuplicateKeyError,
        ValueError,
        KeyError,
    ) as error:
        logger.warning("Checkout admission unavailable (%s)", type(error).__name__)
        raise HTTPException(503, UNAVAILABLE) from None
