#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$PWD/.tools:$PATH"
if [[ ! -x .qwen-venv/bin/python ]]; then
  echo "Install the Studio worker with ./setup.sh --studio first." >&2
  exit 1
fi
case "${1:-}" in
  kitten2)
    mkdir -p .kitten2
    kitten=$(cd .kitten2 && pwd -P)
    # A single installation serves all replicas which mount this model volume.
    exec 9>"$kitten/install.lock"
    flock 9
    if .venv/bin/python -c 'from server.upload_voice import kitten_ready; raise SystemExit(not kitten_ready())'; then
      exit 0
    fi
    rm -f "$kitten/ready"
    .qwen-venv/bin/python -m venv "$kitten/env"
    # Reuse Studio's tested CUDA wheels without mutating the Studio environment.
    site=$("$kitten/env/bin/python" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
    .qwen-venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))' > "$site/reeldub-studio.pth"
    "$kitten/env/bin/python" -m pip install 'kittenml==0.9.7'
    HF_HOME="$kitten/hf" "$kitten/env/bin/python" kitten_voice.py --prepare
    sample=$(mktemp -d)
    trap 'rm -rf "$sample"' EXIT
    echo '["A short English narration."]' > "$sample/lines.json"
    HF_HOME="$kitten/hf" HF_HUB_OFFLINE=1 "$kitten/env/bin/python" kitten_voice.py "$sample"
    test -s "$sample/0.wav"
    touch "$kitten/ready"
    ;;
  graph)
    uv pip install --python .qwen-venv/bin/python --target .tts-graph --no-deps 'faster-qwen3-tts==0.2.4'
    PYTHONPATH=.tts-graph .qwen-venv/bin/python -c 'from faster_qwen3_tts import FasterQwen3TTS'
    ;;
  omni)
    uv venv --allow-existing --python 3.12 .omni-venv
    uv pip install --python .omni-venv/bin/python --torch-backend cu129 'vllm==0.16.0' 'vllm-omni==0.16.0'
    .omni-venv/bin/python -c 'from vllm_omni import Omni'
    ;;
  *) echo "Usage: ./setup-lab.sh graph|omni|kitten2" >&2; exit 2 ;;
esac
