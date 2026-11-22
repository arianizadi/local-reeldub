from server.languages import LANGUAGES
from server.source import MAX_SOURCE_SECONDS
import os
import subprocess

import numpy as np
import soundfile as sf

RATE = 48000


def ff(*args, **kwargs):
    return subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                          check=True, **kwargs)


def fit_voice(path, slot):
    data, rate = sf.read(path, dtype="float32")
    active = np.flatnonzero(np.abs(data) > max(0.003, np.max(np.abs(data)) * 0.014))
    if not len(active):
        raise ValueError("Voice model produced an empty line.")
    data = data[max(0, active[0] - round(0.02 * rate)):active[-1] + round(0.05 * rate)]
    # Pitch-preserving time compression. Chaining moderate factors avoids skipped samples.
    factor = max(1, len(data) / rate / slot)
    filters = []
    while factor > 2:
        filters.append("atempo=2")
        factor /= 2
    filters.append(f"atempo={factor:.6f}")
    raw = ff("-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0", "-af", ",".join(filters),
             "-ar", str(RATE), "-f", "f32le", "pipe:1", input=data.astype("<f4").tobytes(),
             stdout=subprocess.PIPE).stdout
    wave = np.frombuffer(raw, "<f4").copy()[:max(1, round(slot * RATE))]
    if not len(wave):
        raise ValueError("Too little time to fit a spoken line.")
    rms = np.sqrt(np.mean(wave**2))
    wave *= min(4, 0.14 / max(rms, 0.001), 0.90 / max(np.max(np.abs(wave)), 0.001))
    n = min(len(wave) // 2, round(RATE * 0.008))
    if n:
        fade = np.linspace(0, 1, n)
        wave[:n] *= fade
        wave[-n:] *= fade[::-1]
    return wave


def mix(lines, source, folder):
    # Source, mix and timeline are disk-backed. Only a line or a short audio block is mixed in RAM.
    stems = folder / "background.wav"
    separated = stems.exists()
    with sf.SoundFile(stems if separated else source) as track:
        n = len(track)
        voice = np.memmap(folder / "voices.f32", dtype="float32", mode="w+", shape=n)
        for i, line in enumerate(lines):
            next_start = lines[i + 1]["start"] if i + 1 < len(lines) else n / RATE
            slot = min(line["end"] + 0.20, next_start, n / RATE) - line["start"]
            wave = fit_voice(folder / f"{i}.wav", max(0.01, slot))
            start = round(line["start"] * RATE)
            end = min(start + len(wave), n)
            voice[start:end] = wave[:end-start]
            line["spoken_end"] = end / RATE
        with sf.SoundFile(folder / "mix.wav", "w", samplerate=RATE, channels=2,
                          subtype="PCM_16", format="RF64") as out:
            for block in track.blocks(blocksize=RATE, dtype="float32", always_2d=True):
                stop = track.tell()
                span = slice(stop - len(block), stop)
                # The bed spans the full video. Gaps and endings never switch back to the original mix.
                bed = block if separated else (block[:, :1] - block[:, 1:2]) * 0.30
                stereo = np.broadcast_to(bed + voice[span, None], block.shape)
                out.write(np.clip(stereo, -0.98, 0.98))


def clock(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def source_time_limit():
    seconds = os.getenv("REELDUB_MAX_SECONDS")
    return ["-t", str(max(1, min(MAX_SOURCE_SECONDS, int(seconds))))] if seconds else []


def source_input_options():
    # The hosted downloader requests MP4 only. Do not interpret it as a playlist.
    return ["-protocol_whitelist", "file", "-f", "mov"] if os.getenv("REELDUB_SERVICE_LIMITS") else []


def render(media, lines, folder, target="en"):
    tag = "language=" + LANGUAGES[target].mp4
    captions = folder / f"{target}.srt"
    captions.write_text("".join(
        f"{i}\n{clock(line['start'])} --> {clock(line['spoken_end'])}\n{line['text']}\n\n"
        for i, line in enumerate(lines, 1)
    ), encoding="utf-8")
    ff(*source_input_options(), "-i", media, "-i", folder / "mix.wav", "-i", captions,
       *source_time_limit(), "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0", "-map_metadata", "-1",
       "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", "-crf", "19", "-preset", "fast",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-c:s", "mov_text",
       "-metadata:s:a:0", tag, "-metadata:s:s:0", tag,
       "-disposition:s:0", "default", "-movflags", "+faststart", folder / "finished.mp4")
