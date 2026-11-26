"""Persistent CPU text detector with local multilingual and Arabic-script recognition."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unicodedata

import numpy as np

RUNTIME = Path(__file__).resolve().parent / ".screen-ocr"
FILES = ("inference.yml", "inference.json", "inference.pdiparams")
MODELS = {
    "det": ("PP-OCRv5_mobile_det", "0d63e78e2b680928f6b1747d76a08db6e645efb7", (
        "98069072e1b6b37d727fd9d9f11725faa46d6ea0de012f2ed26caea011c37699",
        "05feef1acb00aa4cd7362b15f7f501fc4f99d7b1fa73c1c871e0c7b1504b0f5c",
        "afa1820cb16c1fd0dad589d0f8b389139061c1ef6d68019685fd07be997dda5b")),
    "arabic": ("arabic_PP-OCRv5_mobile_rec", "33d91636a65dca87f5562cc48860332ae367ee1b", (
        "21368419e6c016c31db55d316d59e11c128e1913e6e6fe10287084710043d3a6",
        "d43832804d89c0e18d59f8446a3babd0ef60c44bc335efddac3176caf71d50af",
        "4b4271fd1dd89a40b2056e1a42e58de7c0df2fdcf1a97ca9a8916d7ec45f9143")),
    "general": ("PP-OCRv6_small_rec", "bd619643acac4b9650c040234da8d944476ee3f1", (
        "ab078671bb49f06228eadccd34f1bb501e157f7a047095ffb943ba81512c77d1",
        "f0bf53c853937a917affdd74467472167727f8ab0f0f7bded01c4a16c27e46e6",
        "406e1e689c9a7fbb04178007a3fe10cf852afe7bf8bb3bc6dbb9d532b13bd907")),
}
ENGINE_NAMES = {"arabic": "PP-OCRv5 Arabic", "general": "PP-OCRv6 multilingual"}
MAX_PIXELS = 8_000_000
MAX_LINES = 128


def prepare():
    """Only the setup entry point can download model assets. Verify before publishing each file."""
    for name, (repo, revision, hashes) in MODELS.items():
        folder = RUNTIME / "models" / name
        folder.mkdir(parents=True, exist_ok=True)
        for filename, expected in zip(FILES, hashes):
            dest = folder / filename
            if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == expected:
                continue
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as tmp:
                partial = Path(tmp.name)
            try:
                url = f"https://huggingface.co/PaddlePaddle/{repo}/resolve/{revision}/{filename}"
                subprocess.run(["curl", "-fsSL", "--proto", "=https", "--retry", "3", "--max-time", "180",
                                url, "-o", partial], check=True)
                with partial.open("rb") as downloaded:
                    if hashlib.file_digest(downloaded, "sha256").hexdigest() != expected:
                        raise ValueError(f"Model checksum did not match for {repo}/{filename}")
                os.replace(partial, dest)
            finally:
                partial.unlink(missing_ok=True)


def best_reading(choices):
    # A specialist can confidently transliterate a different script. Prefer an
    # in-alphabet reading, then compare confidence when both look credible.
    arabic = choices.get("arabic")
    general = choices.get("general")
    if not general or not arabic:
        return general or arabic
    text = arabic["text"]
    letters = [c for c in text if c.isalpha()]
    native = letters and sum(unicodedata.name(c, "").startswith("ARABIC") for c in letters) >= .8 * len(letters)
    if native and arabic["confidence"] >= .80 and arabic["confidence"] + .06 >= general["confidence"]:
        return arabic
    if general["text"].strip() and general["confidence"] >= .75:
        return general
    return max((arabic, general), key=lambda r: r["confidence"] if r["text"].strip() else 0)


def model_files(name):
    return all((RUNTIME / "models" / name / filename).is_file() for filename in FILES)


def predict(image, detector, recognizers):
    import cv2
    height, width = image.shape[:2]
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    else:
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    found = next(iter(detector.predict(image, limit_side_len=736, limit_type="max")))["dt_polys"]
    boxes, crops = [], []
    for poly in found[:MAX_LINES]:
        quad = np.array(poly, dtype="float32")
        if quad.shape != (4, 2) or not np.isfinite(quad).all():
            continue
        quad[:, 0] = np.clip(quad[:, 0], 0, width)
        quad[:, 1] = np.clip(quad[:, 1], 0, height)
        tl, tr, br, bl = quad
        w = min(1600, int(max(np.linalg.norm(tl-tr), np.linalg.norm(bl-br))))
        h = min(96, int(max(np.linalg.norm(tl-bl), np.linalg.norm(tr-br))))
        if w < 3 or h < 3:
            continue
        mapping = cv2.getPerspectiveTransform(quad, np.float32([[0, 0], [w, 0], [w, h], [0, h]]))
        crops.append(cv2.warpPerspective(image, mapping, (w,h), borderMode=cv2.BORDER_REPLICATE))
        boxes.append([float(quad[:,0].min() / width), float(quad[:,1].min() / height),
                      float(quad[:,0].max() / width), float(quad[:,1].max() / height)])
    if not crops:
        return []
    choices = [{} for _ in crops]
    for name, recognizer in list(recognizers.items()):
        try:
            results = list(recognizer.predict(crops, batch_size=8))
            if len(results) != len(crops):
                raise ValueError("Incomplete OCR result")
            for group, result in zip(choices, results):
                group[name] = {"text": str(result["rec_text"]).strip()[:512],
                               "confidence": float(result["rec_score"])}
        except (RuntimeError, ValueError, OSError):
            del recognizers[name]
    if not recognizers:
        raise RuntimeError("No prepared text recognizer could run.")
    return [dict(best_reading(group), box=box) for box, group in zip(boxes, choices)]


def serve():
    # Native libraries can print to stdout. Reserve one descriptor for the JSON protocol.
    writer = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    def offline(event, args):
        if event in {"socket.connect", "socket.sendto"}:
            raise PermissionError("Screen OCR uses prepared local models.")
    sys.addaudithook(offline)
    from paddleocr import TextDetection, TextRecognition
    options = dict(device="cpu", cpu_threads=4)
    root = RUNTIME / "models"
    detector = TextDetection(model_name=MODELS["det"][0], model_dir=str(root / "det"),
                             enable_mkldnn=False, **options)
    recognizers = {}
    for name in ENGINE_NAMES:
        if model_files(name):
            try:
                recognizers[name] = TextRecognition(model_name=MODELS[name][0], model_dir=str(root / name),
                                                     enable_mkldnn=name == "arabic", **options)
            except (RuntimeError, ValueError, OSError):
                continue
    if not recognizers:
        raise RuntimeError("Install at least one supported text recognizer.")
    writer.write(json.dumps(list(recognizers)) + "\n")
    raw = sys.stdin.buffer
    while header := raw.readline(80):
        height, width, channels = json.loads(header)
        if not (0 < height <= 8192 and 0 < width <= 8192 and height * width <= MAX_PIXELS and channels in (1,3)):
            raise ValueError("Invalid screen OCR frame")
        data = raw.read(height * width * channels)
        shape = (height, width) if channels == 1 else (height, width, channels)
        image = np.frombuffer(data, dtype=np.uint8).reshape(shape)
        results = predict(image, detector, recognizers)
        writer.write(json.dumps([list(recognizers), results], ensure_ascii=False, allow_nan=False) + "\n")


class Reader:
    def __init__(self, scratch):
        self.process = None
        self.failed = False
        self.scratch = scratch
        self.engines = set()

    def close(self):
        if self.process:
            self.process.kill()
            self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None

    def reply(self, timeout):
        fd = self.process.stdout.fileno()
        deadline = time.monotonic() + timeout
        response = bytearray()
        while len(response) <= 512_000:
            if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
                raise TimeoutError("Screen OCR timed out")
            chunk = os.read(fd, 16384)
            if not chunk:
                raise EOFError("Screen OCR process exited")
            response.extend(chunk)
            if response.endswith(b"\n"):
                return json.loads(response)
        raise ValueError("Invalid OCR response")

    def start(self, scratch):
        env = dict(os.environ, HOME=scratch, XDG_CACHE_HOME=scratch, PADDLE_PDX_CACHE_HOME=scratch,
                   PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True", HF_HUB_OFFLINE="1", CUDA_VISIBLE_DEVICES="",
                   OMP_NUM_THREADS="4", OMP_THREAD_LIMIT="4", MKL_NUM_THREADS="4", OPENBLAS_NUM_THREADS="1")
        self.process = subprocess.Popen([RUNTIME / "bin/python", "-I", "-B", Path(__file__).resolve(), "--serve"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        cwd=scratch, env=env, bufsize=0)
        os.set_blocking(self.process.stdin.fileno(), False)
        if not self.reply(60):
            raise ValueError("Screen OCR could not start")

    def __call__(self, image):
        if self.failed:
            return None
        if image.dtype != np.uint8 or image.ndim not in (2,3) or (image.ndim == 3 and image.shape[2] != 3):
            raise ValueError("Screen OCR needs an RGB or grayscale uint8 frame")
        height, width = image.shape[:2]
        if height * width > MAX_PIXELS or max(height, width) > 8192:
            return None
        try:
            if self.process is None:
                self.start(self.scratch)
            fd = self.process.stdin.fileno()
            deadline = time.monotonic() + 12
            header = json.dumps([height, width, 1 if image.ndim == 2 else 3]).encode() + b"\n"
            for buf in (memoryview(header), memoryview(np.ascontiguousarray(image)).cast("B")):
                while buf:
                    if not select.select([], [fd], [], max(0, deadline - time.monotonic()))[1]:
                        raise TimeoutError("Screen OCR frame write timed out")
                    buf = buf[os.write(fd, buf):]
            names, results = self.reply(35)
            self.engines = {ENGINE_NAMES[n] for n in names}
            return results
        except (EOFError, OSError, ValueError):
            self.failed = True
            self.close()
            print("PaddleOCR unavailable for this render. Continuing with Tesseract.", file=sys.stderr)
            return None


@contextmanager
def paddle_reader(source_language=None):
    """Yield a lazy reader or None. Each job owns and reaps its own CPU process."""
    if not (RUNTIME / "bin/python").is_file() or not model_files("det") or not any(map(model_files, ENGINE_NAMES)):
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="reeldub-ocr-") as scratch:
        reader = Reader(scratch)
        try:
            yield reader
        finally:
            reader.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--prepare"]:
        prepare()
    elif sys.argv[1:] == ["--serve"]:
        serve()
    else:
        raise SystemExit("Run setup-screen-ocr.sh to prepare on-screen translation.")
