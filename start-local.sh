#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

usage() {
  echo "Usage: ./start-local.sh [--postgres] [--check]"
  echo "  --postgres  Start a fresh instance with individual library keys and local PostgreSQL."
  echo "  --check     Validate the render host and Compose settings before installation."
}
postgres=0
check=0
for flag in "$@"; do
  case "$flag" in
    --postgres) postgres=1 ;;
    --check) check=1 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
if [[ -f .env ]]; then
  if grep -Fxq 'REELDUB_DATABASE_MODE=postgres' .env; then
    postgres=1
  elif (( postgres )); then
    echo "This directory already contains an installation. Use a fresh checkout for a new PostgreSQL library; see HOSTING.md to keep both." >&2
    exit 1
  fi
fi
if [[ "$(uname -s):$(uname -m)" != Linux:x86_64 ]]; then
  echo "Run this on the Linux x86-64 NVIDIA render host. Other devices can use its web or iPhone client." >&2
  exit 1
fi
for tool in docker nvidia-smi openssl; do
  command -v "$tool" >/dev/null || { echo "Install $tool on the render host and retry." >&2; exit 1; }
done
docker compose version >/dev/null
docker info >/dev/null
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
docker run --rm --gpus all python:3.12-slim-bookworm nvidia-smi -L >/dev/null

if (( check )); then
  if [[ -f .env ]]; then
    docker compose config --quiet
  else
    opts=(-f compose.yaml)
    if (( postgres )); then opts+=(-f compose.postgres.yaml); fi
    MONGO_ROOT_PASSWORD=check MONGO_PASSWORD=check MONGO_WORKER_PASSWORD=check \
      REELDUB_PG_ROOT_PASSWORD=check REELDUB_PG_APP_PASSWORD=check \
      REELDUB_PG_WORKER_PASSWORD=check REELDUB_PG_KEYS_PASSWORD=check docker compose "${opts[@]}" config --quiet
  fi
  echo "Host and Compose checks passed."
  exit 0
fi
umask 077
if [[ -n "${REELDUB_PORT:-}" ]] && ! [[ "$REELDUB_PORT" =~ ^[1-9][0-9]{0,4}$ && "$REELDUB_PORT" -le 65535 ]]; then
  echo "REELDUB_PORT must be a TCP port between 1 and 65535." >&2
  exit 2
fi
if [[ ! -e .env ]]; then
  (
    set -C
    names=(MONGO_ROOT_PASSWORD MONGO_PASSWORD MONGO_WORKER_PASSWORD)
    if (( postgres )); then
      names+=(REELDUB_PG_ROOT_PASSWORD REELDUB_PG_APP_PASSWORD REELDUB_PG_WORKER_PASSWORD REELDUB_PG_KEYS_PASSWORD)
    else
      names+=(SELF_HOST_KEY)
    fi
    {
      if (( postgres )); then
        echo 'REELDUB_DATABASE_MODE=postgres'
        echo 'COMPOSE_FILE=compose.yaml:compose.postgres.yaml'
        printf 'COMPOSE_PROJECT_NAME=reeldub-pg-%s\n' "$(openssl rand -hex 6)"
      fi
      if [[ -n "${REELDUB_PORT:-}" ]]; then printf 'REELDUB_PORT=%s\n' "$REELDUB_PORT"; fi
      for name in "${names[@]}"; do printf '%s=%s\n' "$name" "$(openssl rand -hex 32)"; done
    } > .env
  )
elif ! grep -q '^MONGO_WORKER_PASSWORD=' .env; then
  printf '\nMONGO_WORKER_PASSWORD=%s\n' "$(openssl rand -hex 32)" >> .env
fi
docker compose config --quiet
docker compose up -d --wait mongo
docker compose exec -T mongo sh /opt/reeldub/configure-worker.sh
echo "Preparing local models. First install can take a while. Follow it in another terminal with: docker compose logs -f models"
docker compose up -d --build
printf 'Open http://%s on this host.\n' "$(docker compose port api 8300)"
if (( postgres )); then
  echo 'Create a private access key for each person with ./access-key create NAME. The key is printed once.'
else
  echo 'Copy SELF_HOST_KEY from .env into ReelDub.'
fi
