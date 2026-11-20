"""Remove vocal residue in flagged regions of the separated music/effects track."""
from difflib import SequenceMatcher
import json
import unicodedata
from pathlib import Path
import sys
from server.gpu import stage

import numpy as np
import soundfile as sf

MODEL = "models/hdemucs_high_trained.pt"
CHECKSUM = "a004b2790d73ffeaa535db458a1a79b539dfdbafbccc31f275d07e632ebd7816"
SOURCES = ["drums", "bass", "other", "vocals"]


def leakage_regions(audio, min_total=0.6):
    from faster_whisper.vad import get_speech_timestamps, VadOptions

    if not len(audio):
        return []
    rate = 16000
    cues = get_speech_timestamps(audio, VadOptions(threshold=0.45, min_speech_duration_ms=150,
                                                   min_silence_duration_ms=240, speech_pad_ms=0))
    # Isolated transient noises can briefly trigger the detector on an otherwise clean bed.
    if sum(cue["end"] - cue["start"] for cue in cues) < min_total * rate:
        return []
    regions = []
    for cue in cues:
        left = max(0, cue["start"] / rate - 0.22)
        right = min(len(audio) / rate, cue["end"] / rate + 0.40)
        if regions and left <= regions[-1][1]:
            regions[-1][1] = right
        else:
            regions.append([left, right])
    return regions


def merge_regions(regions, duration):
    merged = []
    for start, end in sorted(regions):
        start, end = max(0, start), min(duration, end)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def matching_dialogue(original, background, duration):
    """Find recognizable source phrases in the bed, even at an unusual pitch."""
    def normalized(text):
        return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())

    spans = []
    for leak in background:
        if getattr(leak, "avg_logprob", 0) < -0.9:
            continue
        residual = normalized(leak.text)
        for cue in original:
            if leak.end < cue.start - 0.6 or leak.start > cue.end + 0.6:
                continue
            source = normalized(cue.text)
            if min(len(source), len(residual)) < 4:
                continue
            shared = SequenceMatcher(None, source, residual, autojunk=False).find_longest_match().size
            if shared >= max(4, 0.55 * min(len(source), len(residual))):
                # Include the original utterance tail, even if its translation finishes early.
                spans.append((cue.start - 0.18, cue.end + 0.40))
    return merge_regions(spans, duration)


def suppress_unresolved(path, regions):
    """Silence only the bed inside confirmed leaks, with short click-free edges."""
    with sf.SoundFile(path, mode="r+") as track:
        rate = track.samplerate
        for begin, end in regions:
            first, last = max(0, round(begin * rate)), min(len(track), round(end * rate))
            for start in range(first, last, rate):
                track.seek(start)
                block = track.read(min(rate, last-start), dtype="float32", always_2d=True)
                index = np.arange(start, start + len(block))
                gain = np.maximum(np.clip(1 - (index-first)/(rate*0.045), 0, 1) if first else 0,
                                  np.clip(1 - (last-index)/(rate*0.08), 0, 1) if last<len(track) else 0)
                track.seek(start)
                track.write(block * np.asarray(gain).reshape(-1, 1))


def clean(folder, regions):
    import torch
    from torchaudio.functional import resample
    from torchaudio.models import hdemucs_high

    weights = Path(torch.hub.get_dir()) / "torchaudio" / MODEL
    model = hdemucs_high(sources=SOURCES)
    model.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True))
    model.eval().to("cuda")
    path = folder / "background.wav"
    # The file belongs to the unpublished render. Analyze with context from the original
    # bed, writing to a separate file so adjacent blocks never consume processed samples.
    cleaned = folder / "background-clean.wav"
    with sf.SoundFile(path) as src, sf.SoundFile(cleaned, "w", src.samplerate, src.channels,
            subtype="FLOAT", format="RF64") as out, torch.inference_mode():
        rate = src.samplerate
        for part in src.blocks(blocksize=rate * 6, dtype="float32", always_2d=True):
            out.write(part)
        for begin, end in regions:
            first, last = max(0, round(begin * rate)), min(len(src), round(end * rate))
            for start in range(first, last, 6 * rate):
                stop = min(start + 6 * rate, last)
                left = max(start - rate, 0)
                src.seek(left)
                samples = src.read(min(len(src), stop + rate) - left, dtype="float32", always_2d=True)
                pcm = torch.from_numpy(samples.T.copy()).to("cuda")
                audio = resample(pcm, rate, 44100)
                mono = audio.mean(0)
                scale = mono.std(unbiased=False).clamp_min(1e-6)
                stems = model(((audio - mono.mean()) / scale)[None])[0]
                music_fx = resample(stems[:3].sum(0) * scale, 44100, rate)
                center = slice(start - left, stop - left)
                replacement = music_fx.T[center].cpu().numpy()
                original = samples[center]
                index = np.arange(start, stop)
                blend = np.ones(len(index), dtype="float32")
                if first > 0:
                    blend *= np.clip((index - first) / (0.07 * rate), 0, 1)
                if last < len(src):
                    blend *= np.clip((last - index) / (0.14 * rate), 0, 1)
                out.seek(start)
                out.write(original + blend[:, None] * (replacement - original))
    cleaned.replace(path)


if __name__ == "__main__":
    if sys.argv[1] == "--prepare":
        from torchaudio.utils import download_asset
        download_asset(MODEL, hash=CHECKSUM)
    else:
        directory = Path(sys.argv[1])
        with stage("exclusive"):
            print("GPU stage: Cleaning background voices", flush=True)
            clean(directory, json.loads((directory / "leakage.json").read_text()))
