"""Stateless private media inspector. This container has no database or account credentials."""

import json
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .download import download_reel
from .source import reel_url

MAX_BYTES = 80 * 1024 * 1024
slots = BoundedSemaphore(3)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def probe(path):
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise HTTPException(413, "Choose a video under 80 MiB.")
    try:
        raw = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-f",
                "mov",
                "-enable_drefs",
                "0",
                "-use_absolute_path",
                "0",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height,duration",
                "-of",
                "json",
                path,
            ],
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        info = json.loads(raw)
        streams = info["streams"]
        video = next(x for x in streams if x["codec_type"] == "video")
        audio = next(x for x in streams if x["codec_type"] == "audio")
        durations = [
            Decimal(x["duration"])
            for x in [info["format"], video, audio]
            if x.get("duration") not in (None, "N/A")
        ]
        seconds = max(durations)
        if (
            len(streams) > 16
            or video["codec_name"] not in {"h264", "hevc", "av1", "mpeg4", "prores", "vp9"}
            or not 0 < video["width"] * video["height"] <= 4096 * 2304
            or not 0 < seconds <= 600
        ):
            raise ValueError
        return int(seconds.to_integral_value(rounding="ROUND_CEILING"))
    except (ValueError, KeyError, InvalidOperation, StopIteration, subprocess.SubprocessError):
        raise HTTPException(422, "Choose a playable MP4 or MOV with audio, up to 10 minutes and 4K.")


@app.post("/inspect")
async def inspect(request: Request):
    if not slots.acquire(False):
        raise HTTPException(429, "Video intake is busy. Try again shortly.")
    try:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.mov"
            with path.open("wb") as target:
                async for part in request.stream():
                    if target.tell() + len(part) > MAX_BYTES:
                        raise HTTPException(413, "Choose a video under 80 MiB.")
                    target.write(part)
            return {"seconds": await run_in_threadpool(probe, path)}
    finally:
        slots.release()


class SourceFileResponse(FileResponse):
    def __init__(self, path, *, cleanup, headers):
        super().__init__(path, headers=headers, media_type="video/mp4")
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.cleanup()


class Reel(BaseModel):
    url: str


@app.post("/reel")
def fetch(body: Reel):
    if not slots.acquire(False):
        raise HTTPException(429, "Video intake is busy. Try again shortly.")
    tmp = TemporaryDirectory()

    def close():
        tmp.cleanup()
        slots.release()

    handing_off = False
    try:
        folder = Path(tmp.name)
        metadata = folder / "details.json"
        path = download_reel(reel_url(body.url), folder, metadata=metadata, max_mib=MAX_BYTES // (1024 * 1024))
        from urllib.parse import quote

        info = json.loads(metadata.read_text()) if metadata.exists() else {}
        headers = {
            "X-Duration": str(probe(path)),
            "X-Title": quote(info.get("title") or "", safe=""),
            "X-Author": quote(info.get("author") or "", safe=""),
        }
        response = SourceFileResponse(path, headers=headers, cleanup=close)
        handing_off = True
        return response
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise HTTPException(
            422, "Instagram could not provide this Reel. Try importing your own video."
        ) from error
    finally:
        if not handing_off:
            close()
