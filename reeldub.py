import argparse
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

import ctranslate2
import numpy as np
from faster_whisper import WhisperModel

from render import ff, mix, render, RATE
from voices import assign_lines, identify_turns, synthesize, write_references

APP = Path(__file__).resolve().parent


def input_video(value, folder):
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https"):
        domain = (parsed.hostname or "").lower()
        if not any(domain == h or domain.endswith("." + h) for h in ("youtube.com", "youtu.be", "instagram.com")):
            raise ValueError("Paste a YouTube or Instagram video link, or supply a local video file.")
        if parsed.path.rstrip("/") in ("", "/playlist", "/feed", "/reels") or parsed.path.startswith(("/@", "/channel/")):
            raise ValueError("Paste a link to a single video.")
        print("Downloading video…", flush=True)
        limits = ["--max-filesize", "250M", "--socket-timeout", "15", "--retries", "2"] if os.getenv("REELDUB_SERVICE_LIMITS") else []
        result = subprocess.run([
            "yt-dlp", *limits, "--ignore-config", "--no-playlist", "--playlist-end", "1", "--no-progress",
            "--match-filter", "!is_live", "--restrict-filenames",
            "--print", "after_move:filepath", "-o", str(folder / "%(id)s.%(ext)s"), "--", value,
        ], check=True, text=True, stdout=subprocess.PIPE)
        files = result.stdout.strip().splitlines()
        if not files:
            raise ValueError("The link did not return a downloadable video.")
        return Path(files[-1])
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Video file does not exist: {path}")
    return path


def recognize(audio, model_size, device, language, native):
    if device == "auto":
        device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
    print(f"Listening with Whisper {model_size} on {device}…", flush=True)
    model = WhisperModel(model_size, device=device, compute_type="float16" if device == "cuda" else "int8",
                         local_files_only=True)
    segments, info = model.transcribe(audio, language=language, task="transcribe" if native else "translate", word_timestamps=True,
                                      condition_on_previous_text=False, vad_filter=False)
    result = list(segments)
    print(f"Source language: {info.language}. Found {len(result)} segments.", flush=True)
    drafts = []
    if native and info.language != "en":
        translated, _ = model.transcribe(audio, language=info.language, task="translate",
                                         word_timestamps=True, condition_on_previous_text=False,
                                         vad_filter=False)
        drafts = list(translated)
    return result, info.language, drafts


def run(args):
    value = args.input or input("Paste a YouTube or Instagram link (or a video path): ").strip()
    match = args.voice_mode == "match"
    studio = args.background == "separate" or args.translation == "context" or match
    if studio and not (APP / ".qwen-venv/bin/python").is_file():
        raise ValueError("Run ./setup.sh --studio to install the local GPU models.")
    output_dir = args.output.expanduser().resolve().parent if args.output else Path.cwd() / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".reeldub-", dir=output_dir) as temp:
        folder = Path(temp)
        media = input_video(value, folder)
        dest = args.output.expanduser().resolve() if args.output else output_dir / f"{media.stem}.en.mp4"
        if dest.suffix.lower() != ".mp4":
            raise ValueError("Choose an output filename ending in .mp4.")
        if dest.exists():
            raise FileExistsError(f"Output already exists: {dest}. Choose another path with -o.")
        if os.getenv("REELDUB_SERVICE_LIMITS"):
            seconds = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                                    "-of", "csv=p=0", media], text=True).strip())
            if not 0 < seconds <= 600 or media.stat().st_size > 250 * 1024 * 1024:
                raise ValueError("The service accepts Reels up to 10 minutes and 250 MB.")
        source = folder / "source.wav"
        ff("-i", media, "-map", "0:a:0", "-vn", "-ac", "2", "-ar", str(RATE),
           "-c:a", "pcm_s16le", "-rf64", "auto", source)
        analysis_audio = source
        worker = APP / ".qwen-venv/bin/python"
        if args.background == "separate":
            print("Separating original dialogue, music and effects…", flush=True)
            subprocess.run([worker, APP / "separate.py", folder], check=True)
            analysis_audio = folder / "speech.wav"
        def mono(path, name):
            pcm = folder / name
            ff("-i", path, "-ac", "1", "-ar", "16000", "-f", "f32le", pcm)
            return np.memmap(pcm, dtype="<f4", mode="r")

        audio = mono(source, "mix.f32")
        contextual = args.translation == "context"
        segments, language, drafts = recognize(audio, args.model, args.device, args.language, contextual)
        if not (segments or drafts):
            raise ValueError("No translatable speech detected.")
        gc.collect()
        print("Identifying speakers and timing lines…", flush=True)
        speech = mono(analysis_audio, "speech.f32") if analysis_audio != source else audio
        turns = identify_turns(speech)
        lines = []
        candidates = assign_lines(segments, turns)
        # Direct translation can hear a vocalization the native pass omitted, anywhere on the timeline.
        for extra in assign_lines(drafts, turns):
            overlap = sum(max(0, min(extra["end"], cue["end"]) - max(extra["start"], cue["start"]))
                          for cue in candidates)
            if overlap < 0.15 * (extra["end"] - extra["start"]):
                extra["english"] = True
                candidates.append(extra)
        for line in sorted(candidates, key=lambda item: item["start"]):
            line["start"] = max(0, line["start"], lines[-1]["end"] if lines else 0)
            line["end"] = min(len(audio) / 16000, line["end"])
            if line["end"] > line["start"] and any(c.isalnum() for c in line["text"]):
                lines.append(line)
        if not lines:
            raise ValueError("No spoken lines to render.")
        print(f"Rendering {len(lines)} lines across {len({line['speaker'] for line in lines})} speaker group(s).", flush=True)
        if contextual and language != "en":
            print("Translating the source transcript in context…", flush=True)
            script = folder / "lines.json"
            script.write_text(json.dumps(lines, ensure_ascii=False))
            subprocess.run([worker, APP / "translate_lines.py", folder, language], check=True)
            lines = json.loads(script.read_text())
        if match:
            write_references(lines, turns, analysis_audio, folder)
        print("Synthesizing English voices…", flush=True)
        synthesize(lines, folder, match)
        print("Mixing soundtrack…", flush=True)
        mix(lines, source, folder)
        print("Encoding video…", flush=True)
        render(media, lines, folder)
        os.link(folder / "finished.mp4", dest)  # Publish atomically without overwriting another job.
        print(dest)


def main():
    parser = argparse.ArgumentParser(description="Dub a video into English using local models.")
    parser.add_argument("input", nargs="?", help="YouTube URL, Instagram Reel URL, or local video")
    parser.add_argument("-o", "--output", type=Path, help="output MP4 (default: output/<video>.en.mp4)")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    installed = APP / ".venv/reeldub-model"
    parser.add_argument("--model", choices=["small", "medium", "large-v3"],
                        default=installed.read_text().strip() if installed.exists() else "large-v3")
    parser.add_argument("--language", help="optional source language code, for example fa or es")
    studio = (APP / ".qwen-venv/ready").exists()
    parser.add_argument("--background", choices=["separate", "stereo"], default="separate" if studio else "stereo")
    parser.add_argument("--translation", choices=["context", "whisper"], default="context" if studio else "whisper")
    parser.add_argument("--voice-mode", choices=["preset", "match"],
                        default="match" if studio else "preset")
    try:
        run(parser.parse_args())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"ReelDub: {error}\n")


if __name__ == "__main__":
    main()
