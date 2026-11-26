"""Manage private libraries from the self-host operator's terminal."""
import argparse
import hashlib
import os
import secrets
from pathlib import Path
from uuid import uuid4

import psycopg


def digest(key):
    return hashlib.sha256(key.encode()).digest()


def manage(conn, action, label=None):
    if action == "list":
        rows = conn.execute(
            "SELECT label, token_hash IS NOT NULL FROM reeldub.access_keys ORDER BY label"
        ).fetchall()
        return "\n".join(f"{name}\t{'active' if active else 'revoked'}" for name, active in rows)
    if not label or len(label) > 80 or not label.isprintable() or label != label.strip():
        raise ValueError("Choose a label of 1 to 80 printable characters without surrounding spaces.")
    token = None if action == "revoke" else secrets.token_urlsafe(32)
    hashed = digest(token) if token else None
    if action == "create":
        conn.execute(
            "INSERT INTO reeldub.access_keys (library_id,label,token_hash,generation) VALUES (%s,%s,%s,%s)",
            (uuid4(), label, hashed, uuid4()),
        )
    elif action in {"rotate", "revoke"}:
        result = conn.execute(
            "UPDATE reeldub.access_keys SET token_hash=%s, generation=%s WHERE label=%s",
            (hashed, uuid4(), label),
        )
        if not result.rowcount:
            raise ValueError("No library has that label.")
    else:
        raise ValueError("Unknown key action.")
    return token or f"Revoked {label}."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list")
    for action in ("create", "rotate", "revoke"):
        commands.add_parser(action).add_argument("label")
    args = parser.parse_args()
    secret = os.getenv("REELDUB_KEY_ADMIN_DSN_FILE")
    dsn = Path(secret).read_text().strip() if secret else os.getenv("REELDUB_KEY_ADMIN_DSN")
    if not dsn:
        parser.exit(1, "Configure a private PostgreSQL key administrator connection.\n")
    try:
        with psycopg.connect(dsn, sslmode="verify-full", connect_timeout=8,
                             sslrootcert=os.getenv("PGSSLROOTCERT", "/etc/ssl/certs/ca-certificates.crt")) as conn:
            message = manage(conn, args.action, getattr(args, "label", None))
    except psycopg.errors.UniqueViolation:
        parser.exit(1, "That label is already in use. Use rotate to replace its key.\n")
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    except psycopg.Error:
        parser.exit(1, "Key management failed. Check the database, its certificate and the operator credentials.\n")
    print(message)


if __name__ == "__main__":
    main()
