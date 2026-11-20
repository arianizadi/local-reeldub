"""Fetch a canonical Instagram video using the restricted HTTPS proxy."""

import json
import os
import subprocess
from pathlib import Path

from .media_network import instagram_egress
from .source import reel_url


def download_reel(value, folder, *, limited=True, metadata=None, max_mib=250):
    value = reel_url(value)
    print("Downloading video…", flush=True)
    limits = ["--max-filesize", f"{max_mib}M", "--socket-timeout", "15", "--retries", "2"] if limited else []

    if metadata:
        limits.append("--write-info-json")
    with instagram_egress() as proxy:
        options = [
            "--proxy",
            proxy,
            "--use-extractors",
            "Instagram$",
            "--no-plugin-dirs",
            "--no-js-runtimes",
            "--downloader",
            "native",
            "-f",
            "b[protocol=https][ext=mp4]/bv[protocol=https][ext=mp4]+ba[protocol=https][ext=m4a]",
        ]
        result = subprocess.run(
            [
                "yt-dlp",
                *options,
                *limits,
                "--ignore-config",
                "--no-playlist",
                "--playlist-end",
                "1",
                "--no-progress",
                "--match-filter",
                "!is_live",
                "--restrict-filenames",
                "--print",
                "after_move:filepath",
                "-o",
                str(folder / "%(id)s.%(ext)s"),
                "--",
                value,
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            timeout=120,
            env={k: v for k, v in os.environ.items() if k.lower() != "no_proxy"},
        )
    files = result.stdout.strip().splitlines()
    if not files:
        raise ValueError("The link did not return a downloadable video.")
    media = Path(files[-1]).resolve()
    if not media.is_relative_to(folder.resolve()) or not media.is_file():
        raise ValueError("Instagram did not return a video file.")
    if metadata:
        info_path = media.with_suffix(".info.json")
        if info_path.is_file():
            info = json.loads(info_path.read_text())
            title = (info.get("description") or info.get("title") or "").splitlines()
            details = {
                "title": title[0][:140] if title else None,
                "author": (info.get("uploader") or info.get("channel") or "")[:80],
            }
            Path(metadata).write_text(json.dumps(details))
    return media
