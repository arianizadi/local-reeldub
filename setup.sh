#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
model=large-v3
qwen=auto
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) model="${2:?Supply small, medium, or large-v3}"; shift 2 ;;
    --studio|--qwen) qwen=1; shift ;;
    --light) qwen=0; shift ;;
    *) echo "Usage: ./setup.sh [--model small|medium|large-v3] [--studio|--light]" >&2; exit 2 ;;
  esac
done
case "$model" in small|medium|large-v3) ;; *) echo "Unknown translation model: $model" >&2; exit 2 ;; esac
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  if command -v brew >/dev/null; then
    brew install ffmpeg
  elif command -v apt-get >/dev/null; then
    if [[ $EUID == 0 ]]; then apt-get update && apt-get install -y ffmpeg
    else sudo apt-get update && sudo apt-get install -y ffmpeg; fi
  else
    echo "Install FFmpeg (including ffprobe) and run setup again." >&2
    exit 1
  fi
fi
export PATH="$PWD/.tools:$PATH"
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"
if ! command -v uv >/dev/null; then
  mkdir -p .tools
  curl -fsSL --retry 3 https://astral.sh/uv/install.sh -o .tools/install-uv.sh
  UV_UNMANAGED_INSTALL="$PWD/.tools" sh .tools/install-uv.sh
fi
uv sync --frozen --python 3.12
.venv/bin/python assets.py "$model"
if [[ "$qwen" == auto ]]; then
  qwen=0
  if [[ "$(uname -s)" == Linux ]] && command -v nvidia-smi >/dev/null; then
    memory=$(nvidia-smi --id=0 --query-gpu=memory.total --format=csv,noheader,nounits || true)
    if [[ "$memory" =~ ^[0-9]+$ ]] && (( memory >= 15000 )); then qwen=1; fi
  fi
fi
if [[ "$qwen" == 1 ]]; then
  if [[ "$(uname -s)" != Linux ]] || ! command -v nvidia-smi >/dev/null; then
    echo "The Qwen preset requires Linux with an NVIDIA CUDA GPU." >&2
    exit 1
  fi
  uv venv --allow-existing --python 3.12 .qwen-venv
  uv pip install --python .qwen-venv/bin/python --index-url https://download.pytorch.org/whl/cu124 'torch==2.6.0' 'torchaudio==2.6.0'
  uv pip install --python .qwen-venv/bin/python 'qwen-tts==0.1.1' 'bitsandbytes==0.46.1' 'bandit-infer @ git+https://github.com/openmirlab/bandit-infer.git@96ea0ec12aeb444f379c2f5145fb8ee58b58cf19'
  .qwen-venv/bin/python qwen_voice.py --prepare
  .qwen-venv/bin/python translate_lines.py --prepare
  .qwen-venv/bin/python separate.py --prepare
  touch .qwen-venv/ready
else
  rm -f .qwen-venv/ready
fi
printf '%s\n' "$model" > .venv/reeldub-model
echo "Ready. Run ./dub and paste a link."
