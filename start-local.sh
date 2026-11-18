#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
if [[ ! -e .env ]]; then
  (
    set -C
    printf 'MONGO_ROOT_PASSWORD=%s\nMONGO_PASSWORD=%s\nSELF_HOST_KEY=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > .env
  )
fi
if [[ "${1:-}" == "--studio" ]]; then
  docker compose -f compose.yaml -f compose.gpu.yaml up -d --build
elif [[ $# -eq 0 ]]; then
  docker compose up -d --build
else
  echo "Usage: ./start-local.sh [--studio]" >&2
  exit 2
fi
echo "ReelDub runs on localhost port ${REELDUB_PORT:-8300}. Your connection key is in .env."

