"""Run the local Index translator over standard I/O or a per-render Unix socket."""
from contextlib import contextmanager, nullcontext
from difflib import SequenceMatcher
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import unicodedata
import tarfile
import tempfile
import time
from urllib.request import urlopen

ROOT = Path.home() / ".cache/reeldub/index-translate"
SERVER = ROOT / "llama-b12532/llama-server"
COMPLETION = SERVER.with_name("llama-completion")
WEIGHTS = ROOT / "translation.gguf"
MAX_TOKENS = 1400
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


def runtime_env():
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, [str(SERVER.parent), "/usr/local/cuda/lib64",
                                                          env.get("LD_LIBRARY_PATH")]))
    return env


@contextmanager
def runtime():
    # A short socket path also works when the output folder has a long name.
    with tempfile.TemporaryDirectory(prefix="rd-mt-") as temp:
        path = Path(temp) / "translate.sock"
        with subprocess.Popen([SERVER, "--model", WEIGHTS, "--host", path, "--offline", "--no-webui",
                               "-ngl", "99", "-c", "4096", "-np", "1", "--reasoning", "off"],
                              env=runtime_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
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


def offline_completion(instruction, schema):
    # The pinned model's single-user chat template with thinking disabled.
    # Keep control tokens contained in user dialogue as literal JSON escapes.
    content = instruction.replace("<", "\\u003c").replace(">", "\\u003e")
    prompt = "<|im_start|>user\n" + content + "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    try:
        result = subprocess.run(
            [COMPLETION, "--model", WEIGHTS, "--offline", "--no-conversation", "--simple-io",
             "--no-display-prompt", "--color", "off", "--no-escape", "--no-warmup", "--no-context-shift",
             "--reasoning", "off", "--temp", "0", "--device", "CUDA0", "-ngl", "99",
             "-c", "4096", "-n", str(MAX_TOKENS),
             "--json-schema", json.dumps(schema), "--file", "/dev/stdin"],
            input=prompt.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=runtime_env(), timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("The local Index translator timed out.") from None
    if result.returncode:
        raise RuntimeError("The local Index translator failed.")
    # llama-completion also exits zero at the token cap, even after a closing bracket.
    output = result.stdout.rstrip()
    end_marker = b" [end of text]"
    if not output.endswith(end_marker):
        raise ValueError("The local translation stopped before the end of the response.")
    return json.loads(output.removesuffix(end_marker))


def completion(path, instruction, schema):
    if path is None:
        return offline_completion(instruction, schema)
    answer = request(path, "/v1/chat/completions", {
        "messages": [{"role": "user", "content": instruction}], "temperature": 0,
        "max_tokens": MAX_TOKENS, "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "translation", "strict": True, "schema": schema}}})
    choice = answer["choices"][0]
    if choice["finish_reason"] != "stop":
        raise ValueError("The local translation stopped before the end of the response.")
    return json.loads(choice["message"]["content"])


def translation_session():
    return nullcontext() if os.environ.get("REELDUB_OFFLINE_ENGINE") == "1" else runtime()


def translate(lines, source_language, target):
    pending = [i for i, line in enumerate(lines) if not line.get("english")]
    if not pending:
        return
    results = []
    with translation_session() as path:
        for offset in range(0, len(pending), 12):
            batch = pending[offset:offset + 12]
            original = [lines[i]["text"] for i in batch]
            instruction = (f"Translate the following {source_language} dialogue into natural spoken {target}. "
                           "Keep the same JSON array, in order. Preserve meaning, slang, swearing and negation. "
                           "Return only the JSON array.\n\n" + json.dumps(original, ensure_ascii=False))
            schema = {"type": "array", "minItems": len(batch), "maxItems": len(batch),
                      "items": {"type": "string", "minLength": 1}}
            translated = completion(path, instruction, schema)
            if (not isinstance(translated, list) or len(translated) != len(batch)
                    or not all(isinstance(x, str) and x.strip() for x in translated)):
                raise ValueError("The local translation did not preserve all dialogue lines.")
            results.extend(translated)
    for i, text in zip(pending, results, strict=True):
        lines[i]["text"] = text.strip()


def translated_tracks(tracks, target, source_language=None):
    """Keep only legible text that needs a replacement in the selected language."""
    originals = list(dict.fromkeys(t["text"] for t in tracks if any(c.isalpha() for c in t["text"])))
    if not originals:
        return []
    replacements = {}
    # Index was trained with Chinese instruction templates. Audio is only a hint:
    # one video can contain a foreign sign, a local message and English UI.
    from server.languages import LANGUAGES
    language_names = {"en": "英语", "zh": "中文", "fr": "法语", "de": "德语", "it": "意大利语",
                      "ja": "日语", "ko": "韩语", "pt": "葡萄牙语", "ru": "俄语", "es": "西班牙语",
                      "fa": "波斯语", "ar": "阿拉伯语", "ur": "乌尔都语", "hi": "印地语"}
    target_name = next((language_names[code] for code, lang in LANGUAGES.items()
                        if lang.name == target and code in language_names), target)
    audio_hint = (f"视频的对白语言可能是{language_names.get(source_language, source_language)}，但"
                  if source_language else "")

    def normalized(value):
        return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())

    with translation_session() as path:
        for i in range(0, len(originals), 10):
            batch = originals[i:i + 10]
            instruction = (
                f"请将以下屏幕文字翻译成自然简洁的{target_name}，并且严格遵循所有约束要求。\n"
                f"〖源文〗{json.dumps(batch, ensure_ascii=False)}\n"
                f"〖约束要求〗{audio_hint}请独立判断每条文字的实际语言。理解口语和习语的真实含义，"
                "保留原文的语气、情感和否定，不要逐字直译，不要编造原文没有的动作、感官或信息。"
                f"对原本就是{target_name}的文字、无须翻译的名称和商标、无法识别的杂乱 OCR，返回空字符串。"
                "保持相同顺序，每项输出一个字符串。只输出 JSON 数组，不要有任何额外说明。")
            schema = {"type": "array", "minItems": len(batch), "maxItems": len(batch),
                      "items": {"type": "string"}}
            values = completion(path, instruction, schema)
            if not isinstance(values, list) or len(values) != len(batch) or not all(isinstance(v, str) for v in values):
                raise ValueError("The local translator returned incomplete on-screen text.")
            for original, value in zip(batch, values, strict=True):
                value = " ".join(value.split())
                if (value and len(value) <= max(64, len(original) * 5)
                        and any(c.isalpha() for c in value) and normalized(original) != normalized(value)):
                    replacements[original] = value
    # A moving target-language label can be misread during a zoom. A correction
    # by one character is never sufficient evidence by itself to discard a real
    # translation. Require the corrected label at that location in nearby frames.
    unchanged = {}
    for track in tracks:
        if track["text"] not in replacements and "box" in track:
            unchanged.setdefault(normalized(track["text"]), []).append(track)
    result = []
    for track in tracks:
        value = replacements.get(track["text"])
        if not value:
            continue
        before, after = normalized(track["text"]), normalized(value)
        neighbors = unchanged.get(after)
        if neighbors and "box" in track and len(after) >= 4:
            edits = sum(max(b-a, d-c) for op, a, b, c, d in SequenceMatcher(None, before, after).get_opcodes()
                        if op != "equal")
            if edits == 1 and any(max(track["start"], other["start"]) - min(track["end"], other["end"]) <= .5
                                  and max(abs(a-b) for a, b in zip(track["box"], other["box"])) < .12
                                  for other in neighbors):
                continue
        result.append({**track, "translation": value})
    return result


if __name__ == "__main__":
    prepare()
