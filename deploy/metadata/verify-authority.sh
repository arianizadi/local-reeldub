#!/bin/sh
# Installed outside the app release and its encrypted configuration backups.
set -eu
expected=$(cat "${1:?Missing metadata authority file}")
if [ -z "$expected" ] || [ -n "${REELDUB_POSTGRES_DSN_FILE:-}${REELDUB_POSTGRES_DSN:-}" ] ||
   [ "${REELDUB_METADATA_MONGO_DATABASE:-}" != "$expected" ] ||
   [ ! -s "${REELDUB_METADATA_MONGO_URI_FILE:-}" ]; then
    echo "ReelDub metadata does not match the verified post-migration authority. Keep stale restores offline." >&2
    exit 1
fi
# A rolled-back binary also has to understand the new environment variables.
# systemd supplies the active service's WorkingDirectory here.
exec ./venv/bin/python -c 'import sys; from server.db import db, uses_mongo_metadata; sys.exit(0 if uses_mongo_metadata() and db().name == sys.argv[1] else 1)' "$expected"
