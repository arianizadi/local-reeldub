"""Stateless private media inspector. This container has no database or account credentials."""

import json
import subprocess
from math import ceil
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .download import DownloadError, download_reel
from .source import inspect_video, reel_links_enabled
from .video_url import video_url

MAX_BYTES = 80 * 1024 * 1024
DAILY_REEL_BYTES = 250 * 1024 * 1024
slots = BoundedSemaphore(3)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def probe(path, max_bytes=MAX_BYTES):
    if not 0 < path.stat().st_size <= max_bytes:
        raise HTTPException(413, f"Choose a video under {max_bytes // (1024 * 1024)} MiB.")
    try:
        return ceil(inspect_video(path))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


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
    daily: bool = False


@app.post("/reel")
def fetch(body: Reel):
    if not reel_links_enabled():
        raise HTTPException(403, "This server accepts video files from Photos or Files.")
    if not slots.acquire(False):
        raise HTTPException(429, "Video intake is busy. Try again shortly.")
    tmp = None

    def close():
        try:
            if tmp is not None:
                tmp.cleanup()
        finally:
            slots.release()

    handing_off = False
    try:
        try:
            tmp = TemporaryDirectory()
        except OSError as error:
            raise HTTPException(503, "Intake storage is temporarily unavailable.") from error
        folder = Path(tmp.name)
        metadata = folder / "details.json"
        max_bytes = DAILY_REEL_BYTES if body.daily else MAX_BYTES
        path = download_reel(video_url(body.url), folder, metadata=metadata, max_mib=max_bytes // (1024 * 1024))
        from urllib.parse import quote

        info = json.loads(metadata.read_text()) if metadata.exists() else {}
        headers = {
            "X-Duration": str(probe(path, max_bytes)),
            "X-Title": quote(info.get("title") or "", safe=""),
            "X-Author": quote(info.get("author") or "", safe=""),
        }
        response = SourceFileResponse(path, headers=headers, cleanup=close)
        handing_off = True
        return response
    except DownloadError as error:
        raise HTTPException(422, str(error)) from error
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise HTTPException(
            422, "The site could not provide a public video. Try sharing the video file."
        ) from error
    finally:
        if not handing_off:
            close()
