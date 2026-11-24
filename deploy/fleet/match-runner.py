"""File-only transport between the offline render sandbox and the Kitten GPU service."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import wave
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SPOOL = Path(os.environ["REELDUB_KITTEN_MATCH_SPOOL"])
LIMIT = 128 << 20
MAX_CUES = 500
DEADLINE = 900
JOB = re.compile(r"[a-f0-9]{32}\Z")


def read_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Invalid speaker-matching input.")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Speaker-matching input grew beyond its limit.")
        return data


def checked_lines(data):
    if len(data) > (1 << 20):
        raise ValueError("Speaker script is too large.")
    rows = json.loads(data)
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CUES:
        raise ValueError("Invalid number of speaker cues.")
    # The model only receives translated text and the transcript of the reference.
    clean = []
    for row in rows:
        item = {}
        for field in ("text", "reference_text"):
            value = row.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 8000:
                raise ValueError("Invalid speaker cue.")
            item[field] = value
        clean.append(item)
    return clean


def check_audio(data, reference):
    with wave.open(io.BytesIO(data), "rb") as audio:
        rate, frames = audio.getframerate(), audio.getnframes()
        if (audio.getnchannels() != 1 or audio.getsampwidth() != 2 or not 8000 <= rate <= 96000
                or not 0 < frames <= rate * (10 if reference else 180)
                or len(audio.readframes(frames)) != frames * 2):
            raise ValueError("Invalid speaker audio.")


def bundle(path, folder, rows, reference):
    with tempfile.NamedTemporaryFile(dir=SPOOL, prefix=path.stem + "-", suffix=".tmp", delete=False) as buf:
        tmp = Path(buf.name)
    try:
        total = 0
        with zipfile.ZipFile(tmp, "w") as archive:
            if reference:
                archive.writestr("lines.json", json.dumps(rows, ensure_ascii=False))
            for i in range(len(rows)):
                name = ("reference-" if reference else "") + str(i) + ".wav"
                data = read_regular(folder / name, min(LIMIT - total, 18 << 20))
                check_audio(data, reference)
                total += len(data)
                archive.writestr(name, data)
        if tmp.stat().st_size > LIMIT:
            raise ValueError("Speaker audio exceeds the transfer limit.")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def unpack(path, folder, rows=None):
    reference = rows is None
    with zipfile.ZipFile(io.BytesIO(read_regular(path, LIMIT))) as archive:
        files = archive.infolist()
        if len(files) > MAX_CUES + 1 or any(
            f.compress_type != zipfile.ZIP_STORED or f.file_size != f.compress_size
            or f.file_size > (18 << 20) for f in files
        ) or sum(f.file_size for f in files) > LIMIT:
            raise ValueError("Invalid speaker audio bundle.")
        if reference:
            if archive.getinfo("lines.json").file_size > (1 << 20):
                raise ValueError("Speaker script exceeds its size limit.")
            rows = checked_lines(archive.read("lines.json"))
        names = [("reference-" if reference else "") + str(i) + ".wav" for i in range(len(rows))]
        expected = names + (["lines.json"] if reference else [])
        if sorted(f.filename for f in files) != sorted(expected):
            raise ValueError("Incomplete speaker audio bundle.")
        for name in names:
            data = archive.read(name)
            check_audio(data, reference)
            (folder / name).write_bytes(data)
        if reference:
            (folder / "lines.json").write_text(json.dumps(rows, ensure_ascii=False))
        return rows


def active(key):
    try:
        fd = os.open(SPOOL / (key + ".lease"), os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def touch(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Invalid speaker-matching status file.")
        os.utime(fd)
    finally:
        os.close(fd)


def expired(path):
    try:
        return time.time() - path.lstat().st_mtime > DEADLINE
    except FileNotFoundError:
        return False


def ready():
    try:
        stamp = (SPOOL / "heartbeat").stat().st_mtime
    except FileNotFoundError:
        return False
    return time.time() - stamp < 60


def client(folder):
    if not ready():
        raise RuntimeError("The private speaker-matching service is offline.")
    rows = checked_lines(read_regular(folder / "lines.json", 1 << 20))
    key = uuid.uuid4().hex
    lease, request, done, error = [SPOOL / (key + ext) for ext in (".lease", ".req", ".done", ".err")]
    try:
        with lease.open("xb") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            bundle(request, folder, rows, True)
            until = time.monotonic() + DEADLINE
            while not done.exists():
                if error.exists() or not ready() or time.monotonic() > until:
                    raise RuntimeError("The private speaker-matching service could not finish this job.")
                time.sleep(.35)
            unpack(done, folder, rows)
    finally:
        for file in (lease, request, done, error):
            file.unlink(missing_ok=True)


def run_model(key, folder):
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    until = time.monotonic() + DEADLINE - 5
    with subprocess.Popen([ROOT / "kitten-match", folder], env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, start_new_session=True) as child:
        try:
            while child.poll() is None:
                if not active(key) or time.monotonic() > until:
                    raise RuntimeError("Speaker matching was interrupted.")
                touch(SPOOL / "heartbeat")
                time.sleep(1)
            if child.returncode:
                raise RuntimeError("Offline Kitten model failed.")
        finally:
            if child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)


def serve():
    # Both systemd PrivateNetwork and Docker network_mode:none provide this exact topology.
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError("Offline Kitten service requires its own loopback-only network namespace.")
    SPOOL.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (SPOOL / "server.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            touch(SPOOL / "heartbeat")
            for file in SPOOL.iterdir():
                key = file.stem
                if not JOB.fullmatch(key):
                    # Interrupted writers may leave a partial bundle.
                    if file.suffix == ".tmp" and JOB.fullmatch(key.split("-", 1)[0]) and expired(file):
                        file.unlink(missing_ok=True)
                    continue
                if file.suffix != ".req":
                    if file.suffix in (".done", ".err", ".lease") and expired(file) and not active(key):
                        file.unlink(missing_ok=True)
                    continue
                try:
                    if active(key):
                        with tempfile.TemporaryDirectory(prefix="reeldub-clone-") as directory:
                            folder = Path(directory)
                            rows = unpack(file, folder)
                            run_model(key, folder)
                            if active(key):
                                bundle(SPOOL / (key + ".done"), folder, rows, False)
                except Exception as exc:
                    print("Kitten worker failure:", type(exc).__name__, file=sys.stderr, flush=True)
                    if active(key):
                        touch(SPOOL / (key + ".err"))
                finally:
                    file.unlink(missing_ok=True)
            time.sleep(.35)


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        serve()
    elif len(sys.argv) == 2:
        client(Path(sys.argv[1]))
    else:
        raise SystemExit("Expected --serve or a private job directory.")
