"""One isolated yt-dlp session. Its only network peer is the public-media proxy."""
from itertools import islice
import json
from pathlib import Path
import resource
import shutil
import socket
import sys
from urllib.parse import urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.downloader.dash import DashSegmentsFD
from yt_dlp.downloader.hls import HlsFD
from yt_dlp.downloader.http import HttpFD
from yt_dlp.downloader.ism import IsmFD
from yt_dlp.globals import plugin_dirs
from yt_dlp.networking._urllib import UrllibRH
from yt_dlp.utils import DownloadError, determine_protocol

from .source import MAX_SOURCE_SECONDS, SOURCE_ERRORS, TOO_LONG
from .video_url import video_url


class MediaError(ValueError):
    pass


class RecordedHls(HlsFD):
    def real_download(self, filename, info):
        manifest = info.get("hls_media_playlist_data")
        if not manifest:
            with self.ydl.urlopen(self._prepare_url(info, info["url"])) as reply:
                raw = reply.read(4 * 1024**2 + 1)
                if len(raw) > 4 * 1024**2:
                    raise MediaError(SOURCE_ERRORS["oversized_video"])
                manifest = raw.decode("utf-8-sig")
                info["url"] = reply.url
        if "#EXT-X-ENDLIST" not in manifest.splitlines():
            raise MediaError(SOURCE_ERRORS["collection"])
        if not self.can_download(manifest, info):
            raise MediaError(SOURCE_ERRORS["unavailable_video"])
        return super().real_download(filename, {**info, "hls_media_playlist_data": manifest})


DOWNLOADERS = {"http": HttpFD, "https": HttpFD, "http_dash_segments": DashSegmentsFD,
               "m3u8_native": RecordedHls, "m3u8": RecordedHls, "ism": IsmFD}


class VideoDL(YoutubeDL):
    def build_request_director(self, handlers, preferences=None):
        # A single Python transport also lets the socket boundary cover all redirects and fragments.
        return super().build_request_director([UrllibRH])

    def urlopen(self, request):
        video_url(request if isinstance(request, str) else request.url, submission=False)
        return super().urlopen(request)

    def process_ie_result(self, result, download=True, extra_info=None):
        kind = result.get("_type", "video")
        if kind == "compat_list":
            raise MediaError(SOURCE_ERRORS["collection"])
        if kind in ("playlist", "multi_video"):
            # Some generic embedded players wrap a single video in a playlist.
            entries = list(islice(result.get("entries") or (), 2))
            if len(entries) != 1 or not entries[0]:
                raise MediaError(SOURCE_ERRORS["collection"])
            return self.process_ie_result(entries[0], download, extra_info)
        if kind == "video":
            result.pop("additional_urls", None)
            if result.get("is_live") or result.get("live_status") in ("is_live", "is_upcoming", "post_live"):
                raise MediaError(SOURCE_ERRORS["collection"])
            if MAX_SOURCE_SECONDS and (result.get("duration") or 0) > MAX_SOURCE_SECONDS:
                raise MediaError(TOO_LONG)
            if result.get("formats"):
                result["formats"] = [fmt for fmt in result["formats"] if not fmt.get("has_drm")
                                     and determine_protocol(fmt) in DOWNLOADERS]
                if not result["formats"]:
                    raise MediaError(SOURCE_ERRORS["unavailable_video"])
        return super().process_ie_result(result, download, extra_info)


def confine(proxy, deno):
    dest = urlsplit(proxy)
    # The private proxy listener is IPv4, including on dual-stack Docker networks.
    records = socket.getaddrinfo(dest.hostname, dest.port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    peer = records[0][4]
    address, port = peer[:2]
    def audit(event, args):
        if event == "socket.getaddrinfo" and str(args[0]) != address:
            raise PermissionError("Extractor DNS must use the media proxy.")
        if event == "socket.connect" and args[1][:2] != (address, port):
            raise PermissionError("Extractor traffic must use the media proxy.")
        if event in ("os.system", "os.exec", "os.posix_spawn", "socket.sendto"):
            raise PermissionError("Unavailable in media extraction.")
        if event == "subprocess.Popen":
            binary, argv, *_ = args
            # Only the pinned, offline JS challenge solver can run inside extraction.
            # Media demuxing runs separately, on downloaded files, after this process exits.
            if deno and shutil.which(binary) == deno:
                if argv[1:] == ["--version"]:
                    return
                safe_flags = {"--ext=js", "--no-code-cache", "--no-prompt", "--no-remote", "--no-lock",
                              "--node-modules-dir=none", "--no-config", "--cached-only", "--no-npm"}
                if (len(argv) > 3 and argv[1] == "run" and argv[-1] == "-"
                        and {"--no-remote", "--cached-only"} <= set(argv[2:-1]) <= safe_flags):
                    return
            # yt-dlp probes optional ffmpeg executables. It understands a missing binary.
            raise FileNotFoundError("External media programs are unavailable during extraction.")
    sys.addaudithook(audit)
    return f"http://{'[' + address + ']' if ':' in address else address}:{port}"


def extract(params):
    folder = Path(params["folder"])
    size = params["max_bytes"]
    resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
    plugin_dirs.value = []
    deno = shutil.which("deno")
    proxy = confine(params["proxy"], deno)
    options = {"proxy": proxy, "quiet": True, "noprogress": True, "no_warnings": True,
               "cachedir": False, "no_color": True, "noplaylist": True, "ignoreerrors": False,
               "js_runtimes": {"deno": {"path": deno}} if deno else {}, "remote_components": set(),
               "socket_timeout": 15, "retries": 2, "fragment_retries": 2, "max_filesize": size,
               "skip_unavailable_fragments": False, "external_downloader": {"default": "native"},
               "fixup": "never", "format": "best/bestvideo+bestaudio/bestvideo", "format_sort": ["res:1080", "vcodec:h264", "acodec:aac"]}
    with VideoDL(options) as ydl:
        info = ydl.extract_info(params["url"], download=False)
        if not info or info.get("_type", "video") != "video":
            raise MediaError(SOURCE_ERRORS["collection"])
        tracks = info.get("requested_formats") or [info]
        if len(tracks) > 2 or all(track.get("vcodec") == "none" for track in tracks):
            raise MediaError(SOURCE_ERRORS["collection"])
        total = 0
        for index, track in enumerate(tracks):
            track = dict(track)
            video_url(track["url"], submission=False)
            impl = DOWNLOADERS.get(determine_protocol(track))
            if impl is None or track.get("has_drm"):
                raise MediaError(SOURCE_ERRORS["unavailable_video"])
            if total + (track.get("filesize") or 0) > size:
                raise MediaError(SOURCE_ERRORS["oversized_video"])
            def check(progress):
                if total + (progress.get("downloaded_bytes") or 0) > size:
                    raise MediaError(SOURCE_ERRORS["oversized_video"])
            filename = folder / f"track{index}"
            fd = impl(ydl, {**options, "max_filesize": size-total})
            fd.add_progress_hook(check)
            ok, _ = fd.download(str(filename), track)
            if not ok or not filename.is_file():
                raise MediaError(SOURCE_ERRORS["unavailable_video"])
            total += filename.stat().st_size
            if total > size:
                raise MediaError(SOURCE_ERRORS["oversized_video"])
    caption = info.get("description") if info.get("extractor_key") == "Instagram" else None
    title = (caption or info.get("title") or info.get("description") or "").splitlines()
    return {"tracks": len(tracks), "title": title[0][:140] if title else "",
            "author": (info.get("uploader") or info.get("channel") or "")[:80]}


if __name__ == "__main__":
    try:
        result = extract(json.load(sys.stdin))
    except MediaError as err:
        result = {"error": str(err)}
    except DownloadError as err:
        # A site's error can include signed URLs. Return only messages we own.
        restricted = any(hint in str(err).lower() for hint in ("logged-in", "sign in", "login required",
                         "private video", "not available in your country", "drm protected"))
        result = {"error": SOURCE_ERRORS["restricted_video" if restricted else "unavailable_video"]}
    except Exception:
        result = {"error": SOURCE_ERRORS["unavailable_video"]}
    print(json.dumps(result))
