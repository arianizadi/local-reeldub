"""Postgres metadata repository. Media bytes have no representation in this store."""

import os
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from bson import ObjectId
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pymongo.errors import DuplicateKeyError

TABLES = (
    "accounts",
    "beta_allowances",
    "sessions",
    "challenges",
    "jobs",
    "sources",
    "quotes",
    "purchases",
    "subscription_status",
    "billing_bindings",
    "request_rates",
    "account_deletions",
    "playback",
    "counters",
    "store_quarantine",
    "store_history",
    "purchase_intents",
)
S = sql.SQL
MISSING = object()


def encode(value):
    if isinstance(value, datetime):
        return {"$date": value.astimezone(UTC).isoformat(timespec="milliseconds")}
    if isinstance(value, ObjectId):
        return {"$oid": str(value)}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value  # The JSON serializer rejects binary payloads.


def decode(value):
    if isinstance(value, dict):
        if value.keys() == {"$date"}:
            return datetime.fromisoformat(value["$date"])
        if value.keys() == {"$oid"}:
            return ObjectId(value["$oid"])
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def literal(value):
    return sql.Literal(Jsonb(encode(value)))


def column(key):
    if key == "_id":
        return S("id")
    return S("(doc -> {})").format(sql.Literal(key))


def together(parts, join):
    return (
        S("({})").format(S(join).join(parts))
        if parts
        else S("TRUE" if join == " AND " else "FALSE")
    )


def equals(key, value):
    field = column(key)
    if value is None:
        return S("({} IS NULL OR {} = 'null'::jsonb)").format(field, field)
    if key in ("request_keys", "minute_grants", "minute_units") and isinstance(
        value, str
    ):
        return S("({} @> {})").format(field, literal([value]))
    return S("{} = {}").format(field, literal(value))


def predicate(query):
    terms = []
    for key, test in query.items():
        if key in ("$or", "$and"):
            terms.append(
                together(
                    [predicate(q) for q in test], " OR " if key == "$or" else " AND "
                )
            )
        elif not isinstance(test, dict):
            terms.append(equals(key, test))
        else:
            field = column(key)
            for op, value in test.items():
                if op == "$in":
                    terms.append(together([equals(key, v) for v in value], " OR "))
                elif op == "$exists":
                    terms.append(
                        S("{} IS {}NULL").format(field, S("NOT " if value else ""))
                    )
                elif op == "$ne":
                    terms.append(S("({}) IS NOT TRUE").format(equals(key, value)))
                elif op in ("$gt", "$gte", "$lt", "$lte"):
                    sign = {"$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}[op]
                    terms.append(S("{} {} {}").format(field, S(sign), literal(value)))
                elif op == "$regex" and key == "owner" and value == "^apple:":
                    terms.append(S("starts_with(doc->>'owner','apple:')"))
                else:
                    raise ValueError(f"Unsupported metadata predicate {op}")
    return together(terms, " AND ")


def expr(value, doc):
    if isinstance(value, str) and value.startswith("$"):
        return doc.get(value[1:], MISSING)
    if not isinstance(value, dict):
        return value
    op, args = next(iter(value.items()))
    if op == "$literal":
        return args
    if op == "$type":
        return "missing" if expr(args, doc) is MISSING else "present"
    if op == "$cond":
        return expr(args[1] if expr(args[0], doc) else args[2], doc)
    items = [expr(i, doc) for i in args]
    if op == "$ifNull":
        return items[1] if items[0] is MISSING or items[0] is None else items[0]
    if op == "$or":
        return any(items)
    if op == "$eq":
        return items[0] == items[1]
    if op == "$add":
        return sum(items)
    if op in ("$min", "$max"):
        return (min if op == "$min" else max)(
            i for i in items if i is not MISSING and i is not None
        )
    if op == "$setUnion":
        return list(dict.fromkeys(i for group in items for i in group))
    raise ValueError(f"Unsupported metadata expression {op}")


def projected(doc, fields):
    if not fields:
        return doc
    if all(flag == 0 for flag in fields.values()):
        return {k: v for k, v in doc.items() if k not in fields}
    return {k: v for k, v in doc.items() if fields.get(k, k == "_id")}


def apply_update(doc, update, inserting=False):
    row = doc.copy()
    pipeline = isinstance(update, list)
    for stage in update if pipeline else [update]:
        for op, fields in stage.items():
            if op == "$set" or (op == "$setOnInsert" and inserting):
                changes = {
                    k: expr(v, row) if pipeline else v for k, v in fields.items()
                }
                row.update({k: v for k, v in changes.items() if v is not MISSING})
            elif op == "$unset":
                for key in fields:
                    row.pop(key, None)
            elif op == "$inc":
                for key, step in fields.items():
                    row[key] = row.get(key, 0) + step
            elif op == "$max":
                for key, v in fields.items():
                    row[key] = max(row.get(key, v), v)
            elif op == "$project":
                previous = row
                row = projected(previous, fields)
                for key, v in fields.items():
                    if v != 1:
                        row[key] = expr(v, previous)
            elif op != "$setOnInsert":
                raise ValueError(f"Unsupported metadata update {op}")
    return row


def ordering(keys):
    if not keys:
        return S("")
    fields = [
        S("{} {}").format(
            column(k), S("DESC NULLS LAST" if d < 0 else "ASC NULLS FIRST")
        )
        for k, d in keys
    ]
    return S(" ORDER BY ") + S(", ").join(fields)


class Query:
    def __init__(self, collection, query, fields):
        self.collection, self.query, self.fields = collection, query, fields
        self.order, self.maximum = [], 0

    def sort(self, key, direction=None):
        self.order = key if direction is None else [(key, direction)]
        return self

    def limit(self, n):
        self.maximum = n
        return self

    def __iter__(self):
        select = S("doc")
        if self.fields and all(flag == 0 for flag in self.fields.values()):
            select = S("(doc - {}::text[])").format(sql.Literal(list(self.fields)))
        elif self.fields:
            keys = [k for k, include in {"_id": 1, **self.fields}.items() if include]
            select = S("""(SELECT coalesce(jsonb_object_agg(k, doc->k), '{{}}'::jsonb)
                           FROM unnest({}::text[]) AS k WHERE doc ? k)""").format(sql.Literal(keys))
        stmt = S("SELECT {} FROM {} WHERE {}").format(
            select, self.collection.table, predicate(self.query)
        ) + ordering(self.order)
        if self.maximum:
            stmt += S(" LIMIT {}").format(sql.Literal(int(self.maximum)))
        with self.collection.database.read_connection() as conn:
            rows = conn.execute(stmt).fetchall()
        return (decode(row[0]) for row in rows)


@dataclass
class Write:
    matched_count: int = 0
    modified_count: int = 0
    deleted_count: int = 0
    inserted_id: object = None


class Collection:
    def __init__(self, database, name):
        self.database, self.name = database, name
        self.table = sql.Identifier("reeldub", name)

    def find(self, query=None, projection=None):
        return Query(self, query or {}, projection)

    def find_one(self, query, projection=None, sort=None):
        return next(iter(self.find(query, projection).sort(sort or []).limit(1)), None)

    def count_documents(self, query, limit=0):
        stmt = S("SELECT count(*) FROM (SELECT 1 FROM {} WHERE {}").format(
            self.table, predicate(query)
        )
        if limit:
            stmt += S(" LIMIT {}").format(sql.Literal(limit))
        with self.database.read_connection() as conn:
            return conn.execute(stmt + S(") AS matches")).fetchone()[0]

    def distinct(self, key, query=None):
        with self.database.read_connection() as conn:
            rows = conn.execute(
                S("SELECT DISTINCT {} FROM {} WHERE {} AND {} IS NOT NULL").format(
                    column(key), self.table, predicate(query or {}), column(key)
                )
            ).fetchall()
        return [decode(row[0]) for row in rows]

    def _insert(self, conn, doc):
        conn.execute(
            S("INSERT INTO {} (doc) VALUES (%s)").format(self.table),
            (Jsonb(encode(doc)),),
        )

    def insert_one(self, doc):
        with self.database.connection() as conn:
            self._insert(conn, doc)
        return Write(inserted_id=doc["_id"])

    def _change(self, query, update, *, many=False, sort=None, upsert=False):
        with self.database.connection() as conn:
            if upsert:
                if "_id" not in query:
                    raise ValueError("A metadata upsert needs a stable primary key")
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (self.name + str(query["_id"]),),
                )
            stmt = S("SELECT doc FROM {} WHERE {}").format(
                self.table, predicate(query)
            ) + ordering(sort)
            if not many:
                stmt += S(" LIMIT 1")
            stmt += S(
                " FOR UPDATE SKIP LOCKED"
                if sort
                and self.name == "jobs"
                and any(k == "queue_seq" for k, _ in sort)
                else " FOR UPDATE"
            )
            rows = conn.execute(stmt).fetchall()
            changed = 0
            first, last = None, None
            for (raw,) in rows:
                old = decode(raw)
                new = apply_update(old, update)
                if new != old:
                    conn.execute(
                        S("UPDATE {} SET doc=%s WHERE id=%s").format(self.table),
                        (Jsonb(encode(new)), Jsonb(raw["_id"])),
                    )
                    changed += 1
                if first is None:
                    first, last = old, new
            if not rows and upsert:
                last = apply_update(
                    {
                        k: v
                        for k, v in query.items()
                        if not k.startswith("$") and not isinstance(v, dict)
                    },
                    update,
                    True,
                )
                self._insert(conn, last)
            return Write(len(rows), changed), first, last

    def update_one(self, query, update, upsert=False):
        return self._change(query, update, upsert=upsert)[0]

    def update_many(self, query, update):
        return self._change(query, update, many=True)[0]

    def find_one_and_update(
        self,
        query,
        update,
        *,
        upsert=False,
        sort=None,
        projection=None,
        return_document=False,
    ):
        _, before, after = self._change(query, update, upsert=upsert, sort=sort)
        result = after if return_document else before
        return None if result is None else projected(result, projection)

    def find_one_and_delete(self, query):
        with self.database.connection() as conn:
            stmt = S(
                "DELETE FROM {} WHERE id=(SELECT id FROM {} WHERE {} LIMIT 1 FOR UPDATE) RETURNING doc"
            ).format(self.table, self.table, predicate(query))
            row = conn.execute(stmt).fetchone()
        return decode(row[0]) if row else None

    def delete_one(self, query):
        return Write(deleted_count=int(self.find_one_and_delete(query) is not None))

    def delete_many(self, query):
        with self.database.connection() as conn:
            return Write(
                deleted_count=conn.execute(
                    S("DELETE FROM {} WHERE {}").format(self.table, predicate(query))
                ).rowcount
            )


class Postgres:
    def __init__(self, dsn):
        self._profile_reader = ContextVar("reeldub_profile_reader", default=None)
        options = {
            "connect_timeout": 8,
            "application_name": "reeldub",
            "options": "-c statement_timeout=15000 -c lock_timeout=10000",
        }
        if os.getenv("REELDUB_TEST_DATABASE") != "1":
            options.update(
                sslmode="verify-full",
                sslrootcert=os.getenv(
                    "PGSSLROOTCERT", "/etc/ssl/certs/ca-certificates.crt"
                ),
            )
        self.pool = ConnectionPool(
            dsn,
            kwargs=options,
            min_size=0,
            max_size=8,
            timeout=12,
            max_idle=60,
            check=ConnectionPool.check_connection,
            open=True,
        )

    def __getattr__(self, key):
        if key not in TABLES:
            raise AttributeError(key)
        return Collection(self, key)

    @contextmanager
    def connection(self):
        try:
            with self.pool.connection() as conn:
                yield conn
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateKeyError("Conflicting ReelDub record") from exc

    def command(self, name):
        if name != "ping":
            raise ValueError(name)
        with self.connection() as conn:
            # Read an app table as well, to catch a misconfigured schema or grants.
            conn.execute("SELECT id FROM reeldub.jobs LIMIT 0")
        return {"ok": 1}

    @contextmanager
    def profile_reads(self):
        with self.connection() as conn:
            token = self._profile_reader.set(conn)
            try:
                yield
            finally:
                self._profile_reader.reset(token)

    def read_connection(self):
        conn = self._profile_reader.get()
        return nullcontext(conn) if conn is not None else self.connection()

    def live_session(self, digest, stamp):
        with self.connection() as conn:
            row = conn.execute(
                """SELECT s.doc->>'owner', s.doc->>'purpose', d.id IS NOT NULL
                   FROM reeldub.sessions s
                   LEFT JOIN reeldub.account_deletions d ON d.id = s.doc->'owner'
                   WHERE s.id = %s AND s.doc->'expires' > %s""",
                (Jsonb(digest), Jsonb(encode(stamp))),
            ).fetchone()
        if row:
            return {"owner": row[0], "purpose": row[1], "closing": row[2]}
        return None

    def library(self, user, ids=None):
        scope = sql.SQL("")
        params = [Jsonb(user)]
        if ids is not None:
            scope = sql.SQL("AND id = ANY(%s::jsonb[])")
            params.append([Jsonb(value) for value in ids])
        with self.connection() as conn:
            rows = conn.execute(
                sql.SQL("""WITH feed AS MATERIALIZED (
                       SELECT id, doc - ARRAY['minute_units','minute_grants','request_keys','poster'] AS item,
                              coalesce(doc->'last_requested',doc->'created') AS activity
                       FROM reeldub.jobs
                       WHERE doc->'owner'=%s AND doc->'deleted' IS DISTINCT FROM 'true'::jsonb {scope}
                       ORDER BY activity DESC, id DESC LIMIT 100
                   ), positions AS (
                       SELECT id, rank() OVER (ORDER BY doc->'priority' DESC,
                              nullif(doc->'queue_seq','null'::jsonb) ASC NULLS FIRST,
                              CASE WHEN doc->>'queue_seq' IS NULL THEN doc->'created' END,
                              CASE WHEN doc->>'queue_seq' IS NULL THEN id END) - 1 AS ahead
                       FROM reeldub.jobs WHERE doc->'state'='"queued"'::jsonb
                           AND EXISTS (SELECT 1 FROM feed WHERE item->>'state'='queued')
                   )
                   SELECT feed.item, positions.ahead FROM feed LEFT JOIN positions USING (id)
                   ORDER BY feed.activity DESC, feed.id DESC""").format(scope=scope),
                params,
            ).fetchall()
        return [(decode(item), ahead) for item, ahead in rows]

    def expire(self, stamp):
        from datetime import timedelta

        for name in (
            "sessions",
            "challenges",
            "playback",
            "request_rates",
            "account_deletions",
            "quotes",
            "purchase_intents",
        ):
            cutoff = stamp - timedelta(days=1) if name == "quotes" else stamp
            getattr(self, name).delete_many({"expires": {"$lte": cutoff}})


def install(conn):
    conn.execute("CREATE SCHEMA IF NOT EXISTS reeldub")
    for name in TABLES:
        conn.execute(
            S("""CREATE TABLE IF NOT EXISTS {} (
            doc jsonb NOT NULL CHECK (jsonb_typeof(doc)='object' AND NOT (doc ?| ARRAY['poster','phone_hash'])),
            id jsonb GENERATED ALWAYS AS (doc->'_id') STORED PRIMARY KEY)""").format(
                sql.Identifier("reeldub", name)
            )
        )
    conn.execute(Path(__file__).with_name("postgres.sql").read_text())
