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
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--studio" ) ]]; then
  echo "Usage: ./start-local.sh [--studio]" >&2
  exit 2
fi
if ! grep -q '^MONGO_WORKER_PASSWORD=' .env; then
  printf '\nMONGO_WORKER_PASSWORD=%s\n' "$(openssl rand -hex 32)" >> .env
fi
compose=(docker compose)
if [[ "${1:-}" == "--studio" ]]; then
  compose+=(-f compose.yaml -f compose.gpu.yaml)
fi
"${compose[@]}" up -d --wait mongo
"${compose[@]}" exec -T mongo sh /opt/reeldub/configure-worker.sh
"${compose[@]}" up -d --build
echo "ReelDub runs on localhost port ${REELDUB_PORT:-8300}. Your connection key is in .env."
