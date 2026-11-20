#!/bin/sh
set -eu
if [ -f /run/reeldub/ca.pem ]; then set -- --tls --tlsCAFile /run/reeldub/ca.pem; else set --; fi
exec mongosh --quiet --host localhost "$@" --authenticationDatabase admin \
    --username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" \
    --file /opt/reeldub/worker-role.js
