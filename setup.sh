#!/usr/bin/env bash
# Native render tools. For the server and phone app, use start-local.sh.
set -euo pipefail
cd "$(dirname "$0")"
kitten=1
for flag in "$@"; do
  case "$flag" in
    --best-only) kitten=0 ;;
    --studio|--index) ;; # Previous Studio invocations now include Index by default.
    -h|--help) echo "Usage: ./setup.sh [--best-only]"; exit 0 ;;
    *) echo "Usage: ./setup.sh [--best-only]" >&2; exit 2 ;;
  esac
done
if [[ "$(uname -s):$(uname -m)" != Linux:x86_64 ]] || ! command -v nvidia-smi >/dev/null; then
  echo "Voice matching requires a Linux x86-64 host with a compatible NVIDIA CUDA GPU." >&2
  exit 1
fi
for tool in ffmpeg ffprobe curl git; do
  command -v "$tool" >/dev/null || { echo "Install $tool and rerun setup." >&2; exit 1; }
done
if [[ "$kitten" == 1 ]] && ! command -v espeak-ng >/dev/null; then
  echo "Install espeak-ng for the synthetic Kitten installation test and rerun setup." >&2
  exit 1
fi
export PATH="$PWD/.tools:$PATH" UV_LINK_MODE="${UV_LINK_MODE:-copy}"
if ! command -v uv >/dev/null; then
  mkdir -p .tools
  curl -fsSL --retry 3 https://astral.sh/uv/install.sh -o .tools/install-uv.sh
  UV_UNMANAGED_INSTALL="$PWD/.tools" sh .tools/install-uv.sh
fi
uv sync --frozen --python 3.12
.venv/bin/python assets.py large-v3
uv venv --allow-existing --python 3.12 .qwen-venv
uv pip install --python .qwen-venv/bin/python --index-url https://download.pytorch.org/whl/cu124 'torch==2.6.0' 'torchaudio==2.6.0'
uv pip install --python .qwen-venv/bin/python 'qwen-tts==0.1.1' 'bandit-infer @ git+https://github.com/openmirlab/bandit-infer.git@96ea0ec12aeb444f379c2f5145fb8ee58b58cf19'
.qwen-venv/bin/python qwen_voice.py --prepare
.qwen-venv/bin/python index_translate.py
.qwen-venv/bin/python separate.py --prepare
.qwen-venv/bin/python clean_background.py --prepare
touch .qwen-venv/ready
if [[ "$kitten" == 1 ]]; then ./setup-lab.sh kitten-match; fi
echo "Models are ready. Run ./dub 'https://www.instagram.com/reel/SHORTCODE/'"
