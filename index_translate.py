"""Run the optional translation-specialized model on a private per-render socket."""
from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
from urllib.request import urlopen

ROOT = Path.home() / ".cache/reeldub/index-translate"
SERVER = ROOT / "llama-b12532/llama-server"
WEIGHTS = ROOT / "translation.gguf"
REVISION = "645939fbab839a656f1bc39975a872bc8ad749f8"
WEIGHTS_HASH = "46910038bdc6a1c95f18d4e87651ce8df408aa7759757bfac5347bbf69f64439"
RELEASE = "https://github.com/ggml-org/llama.cpp/releases/download/b12532/llama-b12532-bin-ubuntu-cuda-12.8-x64.tar.gz"
RELEASE_HASH = "b8054decd73c7b62ee0b96dd932bd98c558e3fa2244cd17ce73d8c2d18e00c8b"


def verify(path, expected):
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError("Downloaded translation asset failed its checksum.")


def prepare():
    from huggingface_hub import hf_hub_download

    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("The packaged Index CUDA runtime requires Linux x86-64.")
    ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
        directory = Path(tmp)
        if not SERVER.is_file():
            archive = directory / "llama.tar.gz"
            with urlopen(RELEASE, timeout=120) as source, archive.open("wb") as out:
                shutil.copyfileobj(source, out)
            verify(archive, RELEASE_HASH)
            with tarfile.open(archive) as package:
                package.extractall(directory, filter="data")
            (directory / SERVER.parent.name).rename(SERVER.parent)
        if not WEIGHTS.is_file():
            weights = Path(hf_hub_download("IndexTeam/Index-Translate-9B-GGUF", revision=REVISION,
                                           filename="Index-Translate-9B.Q5_K_M.gguf"))
            verify(weights, WEIGHTS_HASH)
            (directory / "model").symlink_to(weights)
            (directory / "model").replace(WEIGHTS)


class LocalHTTP(HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=120)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


def request(path, route, payload=None):
    connection = LocalHTTP(path)
    try:
        connection.request("POST" if payload is not None else "GET", route,
                           body=json.dumps(payload) if payload is not None else None,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Local translation service returned {response.status}.")
        return json.load(response)
    finally:
        connection.close()


@contextmanager
def runtime():
    # A short socket path also works when the output folder has a long name.
    with tempfile.TemporaryDirectory(prefix="rd-mt-") as temp:
        path = Path(temp) / "translate.sock"
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, [str(SERVER.parent), "/usr/local/cuda/lib64",
                                                             env.get("LD_LIBRARY_PATH")]))
        with subprocess.Popen([SERVER, "--model", WEIGHTS, "--host", path, "--offline", "--no-webui",
                               "-ngl", "99", "-c", "4096", "-np", "1", "--reasoning", "off"],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
            try:
                deadline = time.monotonic() + 60
                while child.poll() is None and time.monotonic() < deadline:
                    try:
                        request(path, "/health")
                        break
                    except (OSError, RuntimeError, HTTPException):
                        time.sleep(.25)
                else:
                    raise RuntimeError("The local Index translator did not start.")
                yield path
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()


def translate(lines, source_language, target):
    pending = [i for i, line in enumerate(lines) if not line.get("english")]
    results = []
    with runtime() as path:
        for offset in range(0, len(pending), 12):
            batch = pending[offset:offset + 12]
            original = [lines[i]["text"] for i in batch]
            instruction = (f"Translate the following {source_language} dialogue into natural spoken {target}. "
                           "Keep the same JSON array, in order. Preserve meaning, slang, swearing and negation. "
                           "Return only the JSON array.\n\n" + json.dumps(original, ensure_ascii=False))
            answer = request(path, "/v1/chat/completions", {
                "messages": [{"role": "user", "content": instruction}], "temperature": 0,
                "max_tokens": 1400, "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "dialogue", "strict": True, "schema": {"type": "array", "minItems": len(batch),
                        "maxItems": len(batch), "items": {"type": "string", "minLength": 1}}}}})
            choice = answer["choices"][0]
            translated = json.loads(choice["message"]["content"])
            if (choice["finish_reason"] != "stop" or not isinstance(translated, list) or len(translated) != len(batch)
                    or not all(isinstance(x, str) and x.strip() for x in translated)):
                raise ValueError("The local translation did not preserve all dialogue lines.")
            results.extend(translated)
    for i, text in zip(pending, results, strict=True):
        lines[i]["text"] = text.strip()


if __name__ == "__main__":
    prepare()
