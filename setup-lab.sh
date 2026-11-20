#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$PWD/.tools:$PATH"
if [[ ! -x .qwen-venv/bin/python ]]; then
  echo "Install the Studio worker with ./setup.sh --studio first." >&2
  exit 1
fi
case "${1:-}" in
  graph)
    uv pip install --python .qwen-venv/bin/python --target .tts-graph --no-deps 'faster-qwen3-tts==0.2.4'
    PYTHONPATH=.tts-graph .qwen-venv/bin/python -c 'from faster_qwen3_tts import FasterQwen3TTS'
    ;;
  omni)
    uv venv --allow-existing --python 3.12 .omni-venv
    uv pip install --python .omni-venv/bin/python --torch-backend cu129 'vllm==0.16.0' 'vllm-omni==0.16.0'
    .omni-venv/bin/python -c 'from vllm_omni import Omni'
    ;;
  *) echo "Usage: ./setup-lab.sh graph|omni" >&2; exit 2 ;;
esac
