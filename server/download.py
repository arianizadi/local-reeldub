"""Download a public video through the isolated proxy and normalize its local tracks."""

from http.client import HTTPSConnection
from urllib.parse import urljoin, urlsplit

import json
import hashlib
import re
import os
import subprocess
import sys
import signal
from tempfile import TemporaryDirectory
from pathlib import Path

from .media_network import media_egress
from .source import VideoTooLong, inspect_video
from .video_url import video_url


def resolve_share(value, proxy):
    """Resolve Instagram share tokens only inside the same restricted HTTPS tunnel."""
    endpoint = urlsplit(proxy)
    for _ in range(3):
        if not value.startswith("https://www.instagram.com/share/reel/"):
            return value
        connection = HTTPSConnection(endpoint.hostname, endpoint.port, timeout=10)
        try:
            connection.set_tunnel("www.instagram.com", 443)
            connection.request("GET", urlsplit(value).path, headers={"Host": "www.instagram.com"})
            response = connection.getresponse()
            location = response.getheader("Location")
            if response.status not in (301, 302, 303, 307, 308) or not location:
                raise ValueError("Instagram did not return a public Reel from this share link.")
            value = video_url(urljoin(value, location))
            if not value.startswith(("https://www.instagram.com/reel/", "https://www.instagram.com/share/reel/")):
                raise ValueError("Instagram did not return an individual Reel.")
        finally:
            connection.close()
    if urlsplit(value).path.startswith("/share/reel/"):
        raise ValueError("Instagram's share link did not resolve to a Reel.")
    return value


class DownloadError(ValueError):
    pass


# Only standalone media containers can be demuxed. A local filename is never
# interpreted as a playlist, concatenation script, URL, or external reference.
LOCAL_MEDIA = ["-protocol_whitelist", "file", "-format_whitelist",
               "mov,matroska,webm,mpegts,mpeg,mpegvideo,avi,flv,asf,ogg,aac,mp3,ac3,flac,wav"]


def pack_tracks(tracks, output, max_bytes):
    single = len(tracks) == 1
    if single:
        try:
            inspect_video(tracks[0])
        except VideoTooLong:
            raise
        except ValueError:
            pass
        else:
            tracks[0].replace(output)
            return
    inputs = []
    video = audio = None
    for index, track in enumerate(tracks):
        streams = json.loads(subprocess.check_output(
            ["ffprobe", "-v", "error", *LOCAL_MEDIA, "-show_streams", "-of", "json", track],
            stderr=subprocess.DEVNULL, timeout=20))["streams"]
        for stream in streams:
            kind = stream.get("codec_type")
            if kind == "video" and video is None and not stream.get("disposition", {}).get("attached_pic"):
                if not 0 < stream.get("width", 0) * stream.get("height", 0) <= 4096 * 2304:
                    raise DownloadError("Choose a video up to 4K.")
                video = (f"{index}:{stream['index']}", stream.get("codec_name"))
            if kind == "audio" and audio is None:
                audio = (f"{index}:{stream['index']}", stream.get("codec_name"))
        inputs.extend([*LOCAL_MEDIA, "-i", str(track)])
    if not video:
        raise DownloadError("Choose a recorded video.")
    video_codec = ["-c:v", "copy"] if video[1] in ("h264", "hevc", "av1", "vp9", "mpeg4") else [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-threads", "2"]
    audio_args = []
    if audio:
        codec = ["-c:a", "copy"] if audio[1] == "aac" else ["-c:a", "aac", "-b:a", "128k"]
        audio_args = ["-map", audio[0], *codec]
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-map", video[0], *audio_args,
                    *video_codec, "-map_metadata", "-1", "-map_chapters", "-1",
                    "-movflags", "+faststart", "-fs", str(max_bytes), "-f", "mp4", output],
                   check=True, timeout=120, stderr=subprocess.DEVNULL)
    if output.stat().st_size >= max_bytes:
        raise DownloadError("The video exceeds the configured download size.")
    inspect_video(output)


def download_reel(value, folder, *, metadata=None, max_mib=250):
    value = video_url(value)
    folder = Path(folder).resolve()
    max_bytes = max_mib * 1024**2
    print("Downloading video…", flush=True)
    with TemporaryDirectory(prefix="media-", dir=folder) as temp:
        with media_egress() as proxy:
            value = resolve_share(value, proxy)
            # Run in a clean home with no browser profile, netrc, plugins, or API credentials.
            env = {"HOME": temp, "PATH": os.environ.get("PATH", os.defpath),
                   "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1", "YTDLP_NO_PLUGINS": "1",
                   "PYTHONDONTWRITEBYTECODE": "1", "REELDUB_MAX_SOURCE_SECONDS": os.getenv("REELDUB_MAX_SOURCE_SECONDS", "0")}
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
            params = {"url": value, "proxy": proxy, "folder": temp, "max_bytes": max_bytes}
            with subprocess.Popen([sys.executable, "-m", "server.extract"], cwd=Path(__file__).resolve().parents[1],
                                  env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, start_new_session=True) as proc:
                try:
                    stdout, _ = proc.communicate(json.dumps(params), timeout=240)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()
                    raise DownloadError("The video provider took too long to respond.")
        if proc.returncode:
            raise DownloadError("The video downloader could not finish.")
        result = json.loads(stdout)
        if result.get("error"):
            raise DownloadError(result["error"])
        count = result.get("tracks")
        if count not in (1, 2):
            raise DownloadError("The site did not return a playable video.")
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", result.get("title") or "video").strip("-")[:60] or "video"
        identity = hashlib.sha256(value.encode()).hexdigest()[:12]
        media = folder / f"{name}-{identity}.mp4"
        pack_tracks([Path(temp) / f"track{i}" for i in range(count)], media, max_bytes)
        if metadata:
            Path(metadata).write_text(json.dumps({key: result.get(key) for key in ("title", "author")}))
    return media
