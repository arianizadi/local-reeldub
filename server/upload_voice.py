"""Upload-specific voice routing shared by the scheduler and offline renderer."""

import os
from pathlib import Path
import time

KITTEN_ENGINE = "kitten2"
KITTEN_MODEL = "KittenTTS 2 1.7B"
KITTEN_MATCH = "kitten2_vllm"
KITTEN_MATCH_MODEL = "KittenTTS 2 1.7B vLLM match"
KITTEN_REVISION = "c93cb7c271baa51d9ae8204d5277988906f235a2"
KITTEN_ROOT = Path(__file__).resolve().parents[1] / ".kitten2"
KITTEN_SNAPSHOT = KITTEN_ROOT / "hf/hub/models--KittenML--kitten-tts-2/snapshots" / KITTEN_REVISION


def kitten_ready():
    return ((KITTEN_ROOT / "ready").is_file() and (KITTEN_ROOT / "env/bin/python").is_file()
            and (KITTEN_SNAPSHOT / "config.json").is_file())


def kitten_match_ready(*, require_service=False):
    if require_service:
        spool = os.getenv("REELDUB_KITTEN_MATCH_SPOOL")
        if not spool:
            return False
        try:
            age = time.time() - (Path(spool) / "heartbeat").stat().st_mtime
        except OSError:
            return False
        if not -5 <= age <= 60:
            return False
    return ((KITTEN_ROOT / "vllm-ready").is_file() and (KITTEN_ROOT / "vllm-env/bin/python").is_file()
            and (KITTEN_SNAPSHOT / "speaker/model.safetensors").is_file())


def correct_kitten_model(job, model):
    if job.get("match_engine") == KITTEN_MATCH:
        return model == KITTEN_MATCH_MODEL
    if job.get("narrator_engine") == KITTEN_ENGINE:
        return model == KITTEN_MODEL
    return model not in (KITTEN_MODEL, KITTEN_MATCH_MODEL)


VOICE_ERRORS = {
    "voice_consent": "Match the speaker requires consent for this video. Choose a narrator or confirm permission.",
    "single_speaker": "ReelDub could not isolate one speaker for your own voice. Choose a narrator for this video.",
    "unclear_speaker": "ReelDub could not isolate the speakers clearly enough to match them. Choose a narrator for this video.",
    "upload_translation": "The translation model could not finish this language. Try again later or choose another language.",
}


def upload_mode(job):
    if job.get("voice_mode") is not None:
        return job["voice_mode"]
    # Uploaded jobs queued by older clients also get the privacy-preserving default.
    if (job.get("url") or "").startswith("upload:"):
        return "narrator"
    return None


def voice_scope(job):
    mode = upload_mode(job)
    if mode == "narrator":
        return mode
    consent = job.get("voice_consent")
    version = job.get("voice_consent_version")
    if mode == "match" and type(version) is int and version == 1 and consent in ("own_voice", "documented_permission"):
        return consent
    raise RuntimeError(VOICE_ERRORS["voice_consent"])
