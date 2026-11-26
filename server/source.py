"""Shared input rules for the service, intake and isolated render engine."""
import json
import os
import subprocess
from decimal import Decimal, InvalidOperation

SCREEN_TEXT_VERSION = 1
CAPTION_FILTER_VERSION = 1
MAX_SOURCE_SECONDS = int(os.getenv("REELDUB_MAX_SOURCE_SECONDS", "0"))
if MAX_SOURCE_SECONDS < 0:
    raise ValueError("REELDUB_MAX_SOURCE_SECONDS must be zero or positive.")
TOO_LONG = f"This video exceeds the server's configured limit of {MAX_SOURCE_SECONDS} seconds."

SOURCE_ERRORS = {
    "collection": "Share a link to one recorded video. Collections and live streams cannot be dubbed.",
    "unavailable_video": "The site could not provide a public video. Try sharing the video file.",
    "restricted_video": "The provider requires sign-in or restricts access to this video. Try sharing the video file.",
    "oversized_video": "The video exceeds the configured download size.",
}


def reel_links_enabled():
    return os.getenv("REELDUB_REEL_LINKS", "1") == "1"


class VideoTooLong(ValueError):
    def __init__(self):
        super().__init__(TOO_LONG)


def inspect_video(path):
    """Inspect the original container and tracks before clipping or processing."""
    try:
        raw = subprocess.check_output(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-f", "mov",
             "-enable_drefs", "0", "-use_absolute_path", "0", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height,duration:stream_disposition=attached_pic", "-of", "json", path],
            stderr=subprocess.DEVNULL, timeout=15,
        )
        info = json.loads(raw)
        streams = info["streams"]
        video = next(x for x in streams if x["codec_type"] == "video" and not x.get("disposition", {}).get("attached_pic"))
        if (len(streams) > 16
                or video["codec_name"] not in {"h264", "hevc", "av1", "mpeg4", "prores", "vp9"}
                or not 0 < video["width"] * video["height"] <= 4096 * 2304):
            raise ValueError
        durations = [Decimal(x["duration"]) for x in [info["format"], *streams]
                     if x.get("duration") not in (None, "N/A")]
        seconds = max(durations)
        if not seconds.is_finite() or seconds <= 0:
            raise ValueError
    except (ValueError, TypeError, KeyError, InvalidOperation, StopIteration, subprocess.SubprocessError) as exc:
        raise ValueError("Choose a playable MP4 or MOV, up to 4K.") from exc
    if MAX_SOURCE_SECONDS and seconds > MAX_SOURCE_SECONDS:
        raise VideoTooLong()
    return seconds
