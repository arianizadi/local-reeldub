"""Output languages supported by the installed Studio translator and speech engines."""
import os
from typing import NamedTuple


class Language(NamedTuple):
    name: str
    mp4: str
    matched: bool = True
    preset_lang: str = ""
    voices: tuple[str, ...] = ()


LANGUAGES = {
    "en": Language("English", "eng", True, "en-us", ("af_heart", "am_fenrir", "bf_emma", "bm_george", "af_bella", "am_michael")),
    "zh": Language("Chinese", "zho"),
    "fr": Language("French", "fra", True, "fr-fr", ("ff_siwis",)),
    "de": Language("German", "deu"),
    "hi": Language("Hindi", "hin", False, "hi", ("hf_alpha", "hm_omega", "hf_beta", "hm_psi")),
    "it": Language("Italian", "ita", True, "it", ("if_sara", "im_nicola")),
    "ja": Language("Japanese", "jpn"),
    "ko": Language("Korean", "kor"),
    "pt": Language("Portuguese", "por", True, "pt-br", ("pf_dora", "pm_alex", "pm_santa")),
    "ru": Language("Russian", "rus"),
    "es": Language("Spanish", "spa", True, "es", ("ef_dora", "em_alex", "em_santa")),
}


def default_render_quality():
    """Studio admissions request the explicit Best preset. Light uses its installed models."""
    return "best" if os.getenv("REELDUB_OUTPUT_PROFILE") == "studio" else None


def outputs(profile):
    return list(LANGUAGES) if profile == "studio" else ["en"]
