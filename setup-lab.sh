#!/usr/bin/env bash
# Install and exercise the Kitten speaker matcher using a synthetic voice.
set -euo pipefail
cd "$(dirname "$0")"
if [[ $# != 1 || "$1" != kitten-match ]]; then
  echo "Usage: ./setup-lab.sh kitten-match" >&2
  exit 2
fi
if [[ ! -x .qwen-venv/bin/python || ! -x .venv/bin/python ]]; then
  echo "Set up the base renderer with ./setup.sh --best-only first." >&2
  exit 1
fi
export PATH="$PWD/.tools:$PATH"
mkdir -p .kitten2
kitten=$(cd .kitten2 && pwd -P)
exec 9>"$kitten/install.lock"
flock 9
if .venv/bin/python -c 'import os; from server.upload_voice import kitten_match_ready; raise SystemExit(not kitten_match_ready(require_service=bool(os.getenv("REELDUB_KITTEN_MATCH_SPOOL"))))'; then
  exit 0
fi
command -v espeak-ng >/dev/null || { echo "Install espeak-ng to generate the local test reference." >&2; exit 1; }
uv venv --allow-existing --python 3.12 "$kitten/vllm-env"
wheel=https://github.com/vllm-project/vllm/releases/download/v0.31.0/vllm-0.31.0+cu129-cp38-abi3-manylinux_2_28_x86_64.whl
uv pip install --python "$kitten/vllm-env/bin/python" --torch-backend cu129 'kittenml[vllm]==0.9.8' "$wheel"
# Kitten reads WAV references with soundfile in this environment.
uv pip uninstall --python "$kitten/vllm-env/bin/python" torchcodec
# Use the cuDNN build verified with this vLLM/CUDA pair. Load it only for
# Kitten; installing it into Qwen's environment would replace Qwen's CUDA stack.
if [[ ! -e "$kitten/system-cudnn/libcudnn_graph.so.9" ]]; then
  uv pip install --python "$kitten/vllm-env/bin/python" --target "$kitten/cudnn-runtime" --no-deps 'nvidia-cudnn-cu12==9.25.0.15'
  mkdir -p "$kitten/system-cudnn"
  for lib in "$kitten"/cudnn-runtime/nvidia/cudnn/lib/libcudnn*.so.9; do
    test -f "$lib"
    ln -sf "$lib" "$kitten/system-cudnn/"
  done
fi
HF_HOME="$kitten/hf" "$kitten/vllm-env/bin/python" kitten_voice.py --prepare

sample=$(mktemp -d)
trap 'rm -rf "$sample"' EXIT
reference="Today the little blue bicycle is waiting beside the garden gate."
espeak-ng -v en-us -w "$sample/reference-0.wav" "$reference"
printf '[{"text":"This is a local voice test.","reference_text":"%s"}]\n' "$reference" > "$sample/lines.json"
if [[ -n ${REELDUB_KITTEN_MATCH_SPOOL:-} ]]; then
  python3 deploy/fleet/match-runner.py "$sample"
else
  ./kitten-match "$sample"
fi
test -s "$sample/0.wav"
touch "$kitten/vllm-ready"
echo "Kitten speaker matching is ready."
