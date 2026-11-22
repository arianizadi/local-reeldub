"""The two known-compatible Studio stages can overlap on a single 16 GiB GPU."""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import sys


def lock_dir():
    path = Path(os.getenv("REELDUB_GPU_LOCK_DIR", "~/.cache/reeldub/gpu0")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def pipeline_slot():
    if sys.platform != "linux" or os.getenv("REELDUB_GPU_SCHEDULER") != "1":
        yield True
        return
    import fcntl
    slots = os.getenv("REELDUB_GPU_PIPELINE_SLOTS", "2")
    if slots not in {"1", "2"}:
        raise ValueError("REELDUB_GPU_PIPELINE_SLOTS must be 1 or 2")
    directory = lock_dir()
    for slot in range(int(slots)):
        with (directory / f"pipeline-{slot}").open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            yield True
            return
    yield False


@contextmanager
def stage(kind):
    if sys.platform != "linux":
        yield
        return
    import fcntl
    lanes = {"exclusive": ("translation", "voice"), "translation": ("translation",), "voice": ("voice",)}
    with ExitStack() as stack:
        for lane in lanes[kind]:
            file = stack.enter_context((lock_dir() / lane).open("a"))
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Waiting for GPU capacity…", flush=True)
                fcntl.flock(file, fcntl.LOCK_EX)
        yield


@contextmanager
def transcription_device(requested, cuda_available, overflow=False):
    if requested == "cpu" or not cuda_available or sys.platform != "linux":
        yield requested if requested != "auto" else ("cuda" if cuda_available else "cpu")
        return
    if overflow:
        import fcntl
        # Release any partial reservation before trying CPU. A waiting free job
        # must never hold the voice lane or the translation lane on its own.
        with ExitStack() as locks:
            try:
                for name in ("translation", "voice"):
                    file = locks.enter_context((lock_dir() / name).open("a"))
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                yield "cuda"
                return
        with (lock_dir() / "cpu-transcription").open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                yield "cpu"
                return
    with stage("exclusive"):
        yield "cuda"
