"""The public service accepts only individual Instagram Reel permalinks."""
import re

REEL = re.compile(r"https://(?:(?:www|m)\.)?instagram\.com/reels?/([A-Za-z0-9_-]{5,32})/?(?:[?#][^\s]*)?", re.ASCII | re.I)


def reel_url(value):
    match = REEL.fullmatch(value.strip())
    if not match or len(value) > 2048:
        raise ValueError("Paste a link to an individual Instagram Reel.")
    return f"https://www.instagram.com/reel/{match[1]}/"
