#!/usr/bin/env bash
# A separate CPU runtime keeps OCR independent of the CUDA voice and translation stack.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$PWD/.tools:$PATH" UV_LINK_MODE="${UV_LINK_MODE:-copy}"
uv venv --allow-existing --python 3.12 .screen-ocr
uv pip install --python .screen-ocr/bin/python 'paddlepaddle==3.3.1' 'paddleocr==3.7.0'
.venv/bin/python paddle_screen.py --prepare
.venv/bin/python - <<'PYTHON'
import numpy as np
from paddle_screen import paddle_reader

with paddle_reader("fa") as read:
    if read is None or read(np.full((64, 96), 255, dtype=np.uint8)) is None:
        raise SystemExit("PP-OCRv5 could not read a test frame with the prepared CPU runtime.")
PYTHON
