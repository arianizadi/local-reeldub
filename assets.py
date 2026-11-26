"""Download and check the three ONNX assets used by the local pipeline."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import sys
import tempfile

from faster_whisper.utils import download_model

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "local-reeldub"
SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"
SPEAKER = "wespeaker_en_voxceleb_resnet34.onnx"
SEG_ARCHIVE = "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
ASSETS = {
    SEG_ARCHIVE: ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models",
                  "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"),
    SPEAKER: (SHERPA, "5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94"),
}


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def prepare(model):
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, (base, checksum) in ASSETS.items():
        path = CACHE / name
        if path.is_file() and digest(path) == checksum:
            continue
        print(f"Downloading {name}", flush=True)
        with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as tmp:
            partial = Path(tmp.name)
        try:
            subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", partial, f"{base}/{name}"], check=True)
            if digest(partial) != checksum:
                raise RuntimeError(f"Checksum mismatch for {name}. Run setup again.")
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    with tarfile.open(CACHE / SEG_ARCHIVE) as archive:
        member = archive.extractfile("sherpa-onnx-pyannote-segmentation-3-0/model.onnx")
        with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as tmp:
            shutil.copyfileobj(member, tmp)
        Path(tmp.name).replace(CACHE / "segmentation.onnx")
    print(f"Preparing Whisper {model}", flush=True)
    download_model(model)


if __name__ == "__main__":
    prepare(sys.argv[1])
