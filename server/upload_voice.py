"""Upload-specific voice routing shared by the scheduler and offline renderer."""

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
