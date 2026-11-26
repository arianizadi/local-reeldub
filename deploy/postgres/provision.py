"""Create per-service PostgreSQL roles and update the ReelDub schema atomically."""
import os
from pathlib import Path

import psycopg
from psycopg import sql

from server.postgres import LOCAL_TABLES, install

ROLES = {
    "reeldub_app": "REELDUB_PG_APP_PASSWORD",
    "reeldub_worker": "REELDUB_PG_WORKER_PASSWORD",
    "reeldub_keys": "REELDUB_PG_KEYS_PASSWORD",
}


def prepare():
    with psycopg.connect(host="postgres", dbname="reeldub", user="postgres",
                         password=os.environ["POSTGRES_PASSWORD"], sslmode="verify-full",
                         sslrootcert=os.environ["PGSSLROOTCERT"], connect_timeout=10) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(742305903631228055::bigint)")
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='reeldub_schema'").fetchone():
            conn.execute("CREATE ROLE reeldub_schema NOLOGIN")
        for role, env_name in ROLES.items():
            name = sql.Identifier(role)
            password = os.environ[env_name]
            if len(password) < 32:
                raise ValueError("Use generated database credentials of at least 32 characters.")
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(name))
            conn.execute(sql.SQL("ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                                 "NOBYPASSRLS PASSWORD {}").format(name, sql.Literal(password)))
        conn.execute("REVOKE ALL ON DATABASE reeldub FROM PUBLIC")
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute("GRANT CONNECT ON DATABASE reeldub TO reeldub_app, reeldub_worker, reeldub_keys")
        conn.execute("CREATE SCHEMA IF NOT EXISTS reeldub AUTHORIZATION reeldub_schema")
        conn.execute("GRANT CREATE ON DATABASE reeldub TO reeldub_schema")
        conn.execute("SET LOCAL ROLE reeldub_schema")
        install(conn, local=True)
        conn.execute(Path("server/local_auth.sql").read_text())
        conn.execute("GRANT USAGE ON SCHEMA reeldub TO reeldub_app, reeldub_worker, reeldub_keys")
        # Grants are enumerated so future authentication tables stay private.
        for table in LOCAL_TABLES:
            conn.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON {} TO reeldub_app").format(
                sql.Identifier("reeldub", table)))
        conn.execute("GRANT SELECT,UPDATE ON reeldub.jobs TO reeldub_worker")
        conn.execute("GRANT SELECT (library_id, token_hash, generation) ON reeldub.access_keys TO reeldub_app")
        conn.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON reeldub.access_keys TO reeldub_keys")
        conn.execute("RESET ROLE")
        conn.execute("REVOKE CREATE ON DATABASE reeldub FROM reeldub_schema")
    print("PostgreSQL schema and service roles ready.")


if __name__ == "__main__":
    prepare()
