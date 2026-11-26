#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

usage() {
  echo "Usage: ./start-local.sh [--check]"
  echo "  --check  Validate the host, NVIDIA container runtime and configuration without starting ReelDub."
}
case "${1:-}" in
  ""|--check) ;;
  --help|-h) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if (( $# > 1 )); then usage >&2; exit 2; fi
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
# Check the same GPU device request used by Compose before starting large downloads.
docker run --rm --gpus all python:3.12-slim-bookworm nvidia-smi -L >/dev/null

umask 077
prepare_env() {
  if [[ ! -e .env ]]; then
    (
      set -C
      for name in MONGO_ROOT_PASSWORD MONGO_PASSWORD MONGO_WORKER_PASSWORD SELF_HOST_KEY; do
        printf '%s=%s\n' "$name" "$(openssl rand -hex 32)"
      done > .env
    )
  elif ! grep -q '^MONGO_WORKER_PASSWORD=' .env; then
    printf '\nMONGO_WORKER_PASSWORD=%s\n' "$(openssl rand -hex 32)" >> .env
  fi
}
if [[ "${1:-}" == --check ]]; then
  if [[ -f .env ]]; then
    docker compose config --quiet
  else
    MONGO_ROOT_PASSWORD=check MONGO_PASSWORD=check MONGO_WORKER_PASSWORD=check SELF_HOST_KEY=check docker compose config --quiet
  fi
  echo "Host and Compose checks passed. Run ./start-local.sh to install."
  exit 0
fi
prepare_env
docker compose config --quiet
docker compose up -d --wait mongo
# Also update the scoped worker role in a database initialized by an older release.
docker compose exec -T mongo sh /opt/reeldub/configure-worker.sh
echo "Preparing local models. First install can take a while. Follow it in another terminal with: docker compose logs -f models"
docker compose up -d --build
printf 'Open http://%s on this host. Copy SELF_HOST_KEY from .env into ReelDub.\n' "$(docker compose port api 8300)"
