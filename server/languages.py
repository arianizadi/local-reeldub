"""Output languages supported by ReelDub speaker matching."""
from typing import NamedTuple


class Language(NamedTuple):
    name: str
    mp4: str
    matched: bool = True


LANGUAGES = {
    "en": Language("English", "eng"),
    "zh": Language("Chinese", "zho"),
    "fr": Language("French", "fra"),
    "de": Language("German", "deu"),
    "it": Language("Italian", "ita"),
    "ja": Language("Japanese", "jpn"),
    "ko": Language("Korean", "kor"),
    "pt": Language("Portuguese", "por"),
    "ru": Language("Russian", "rus"),
    "es": Language("Spanish", "spa"),
}


def outputs(profile):
    return list(LANGUAGES) if profile == "studio" else []
