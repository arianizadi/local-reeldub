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

from server.progress import report
from server.gpu import transcription_device
from server.languages import LANGUAGES
from server.source import inspect_video, reel_url
from server.download import download_reel
from clean_background import leakage_regions, matching_dialogue, merge_regions, suppress_unresolved
from render import ff, mix, render, source_input_options, source_time_limit, RATE
from voices import assign_lines, identify_turns, synthesize, write_references

APP = Path(__file__).resolve().parent


def input_video(value, folder):
    hosted = bool(os.getenv("REELDUB_SERVICE_LIMITS"))
    if hosted and os.getenv("REELDUB_STAGED_INPUT") == value:
        return Path(value)
    if hosted or urlparse(value).scheme:
        value = reel_url(value)
        return download_reel(value, folder, limited=hosted, metadata=os.getenv("REELDUB_METADATA_FILE"))
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Video file does not exist: {path}")
    return path


def whisper_model(model_size, device):
    threads = 6 if device == "cpu" and os.getenv("REELDUB_FREE_CPU_ASR") == "1" else 0
    return WhisperModel(model_size, device=device, compute_type="float16" if device == "cuda" else "int8",
                        local_files_only=True, cpu_threads=threads)


def recognize(audio, model_size, device, language, native, target="en", bed=None, beam=5):
    if device == "auto":
        device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
    print(f"Listening with Whisper {model_size} on {device}…", flush=True)
    model = whisper_model(model_size, device)
    segments, info = model.transcribe(audio, beam_size=beam, language=language, task="transcribe" if native else "translate", word_timestamps=True,
                                      condition_on_previous_text=False, vad_filter=False)
    result = list(segments)
    print(f"Source language: {info.language}. Found {len(result)} segments.", flush=True)
    drafts = []
    if native and target == "en" and info.language != "en":
        translated, _ = model.transcribe(audio, beam_size=beam, language=info.language, task="translate",
                                         word_timestamps=True, condition_on_previous_text=False,
                                         vad_filter=False)
        drafts = list(translated)
    leaks = []
    if bed is not None:
        recognized, _ = model.transcribe(bed, beam_size=beam, language=info.language, task="transcribe" if native else "translate",
                                        condition_on_previous_text=False, word_timestamps=True, vad_filter=False)
        leaks = matching_dialogue(result, recognized, len(bed) / 16000)
    return result, info.language, drafts, leaks


def timed_lines(candidates, duration):
    lines = []
    for cue in sorted(candidates, key=lambda item: item["start"]):
        start = max(0, cue["start"], lines[-1]["end"] if lines else 0)
        end = min(duration, cue["end"])
        if end > start and any(c.isalnum() for c in cue["text"]):
            lines.append({**cue, "start": start, "end": end})
    return lines


def run(args):
    value = args.input or input("Paste an Instagram Reel link (or a video path): ").strip()
    target = args.target_language
    output = LANGUAGES[target]
    beam = 5
    if args.quality:
        args.model = "large-v3"
        args.background = "separate"
        args.translation = "whisper" if args.quality == "fast" and target == "en" else "context"
        args.voice_mode = "preset" if args.quality in ("medium", "fast") and output.voices else "auto"
        if args.lab_voice:
            args.voice_mode = "preset" if args.lab_voice == "preset" else "match"
        beam = 1 if args.quality == "fast" else 5
        os.environ["REELDUB_QWEN_SIZE"] = "0.6B" if args.lab_voice == "small" else "1.7B"
        os.environ["REELDUB_TTS_BATCH_SIZE"] = str(args.voice_batch or os.getenv("REELDUB_TTS_BATCH_SIZE") or 2)
    matched_default = (APP / ".qwen-venv/ready").is_file() and output.matched
    match = matched_default if args.voice_mode == "auto" else args.voice_mode == "match"
    if match and not output.matched:
        raise ValueError(f"{output.name} uses a preset voice. Select --voice-mode auto.")
    if not match and not output.voices:
        raise ValueError(f"{output.name} requires the Studio voice matching model.")
    if target != "en" and args.translation != "context":
        raise ValueError("Install Studio and use --translation context for this output language.")
    studio = args.background == "separate" or args.translation == "context" or match
    if studio and not (APP / ".qwen-venv/bin/python").is_file():
        raise ValueError("Run ./setup.sh --studio to install the local GPU models.")
    output_dir = args.output.expanduser().resolve().parent if args.output else Path.cwd() / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".reeldub-", dir=output_dir) as temp:
        folder = Path(temp)
        media = input_video(value, folder)
        dest = args.output.expanduser().resolve() if args.output else output_dir / f"{media.stem}.{target}.mp4"
        if dest.suffix.lower() != ".mp4":
            raise ValueError("Choose an output filename ending in .mp4.")
        if dest.exists():
            raise FileExistsError(f"Output already exists: {dest}. Choose another path with -o.")
        if os.getenv("REELDUB_SERVICE_LIMITS"):
            if media.stat().st_size > 250 * 1024 * 1024:
                raise ValueError("Choose a video under 250 MiB.")
            report(duration=float(inspect_video(media)))
        source = folder / "source.wav"
        ff(*source_input_options(), "-i", media, *source_time_limit(), "-map", "0:a:0", "-vn", "-ac", "2", "-ar", str(RATE),
           "-c:a", "pcm_s16le", "-rf64", "auto", source)
        analysis_audio = source
        worker = APP / ".qwen-venv/bin/python"
        if args.background == "separate":
            print("Separating original dialogue, music and effects…", flush=True)
            subprocess.run([worker, APP / "separate.py", folder, args.lab_separation], check=True)
            analysis_audio = folder / "speech.wav"
        def mono(path, name):
            pcm = folder / name
            ff("-i", path, "-ac", "1", "-ar", "16000", "-f", "f32le", pcm)
            return np.memmap(pcm, dtype="<f4", mode="r")

        bed = mono(folder / "background.wav", "background.f32") if args.background == "separate" else None
        audio = mono(source, "mix.f32")
        contextual = args.translation == "context"
        overflow = args.device == "auto" and os.getenv("REELDUB_FREE_CPU_ASR") == "1"
        with transcription_device(args.device, ctranslate2.get_cuda_device_count() > 0, overflow) as device:
            segments, language, drafts, confirmed = recognize(audio, args.model, device, args.language,
                                                             contextual, target, bed, beam)
        if bed is not None:
            regions = merge_regions(leakage_regions(bed) + confirmed, len(bed) / 16000)
            if regions:
                print("Cleaning residual voices from the background…", flush=True)
                (folder / "leakage.json").write_text(json.dumps(regions))
                subprocess.run([worker, APP / "clean_background.py", folder], check=True)
                if confirmed:
                    cleaned = mono(folder / "background.wav", "clean.f32")
                    remaining = leakage_regions(cleaned, min_total=0)
                    def contains_speech(span):
                        return any(c < span[1] and d > span[0] for c, d in remaining)
                    quiet = [span for span in confirmed if not contains_speech(span)]
                    if quiet:
                        # A distorted voice can evade the same acoustic detector twice.
                        # Check only the previously matched dialogue windows after separation.
                        with transcription_device(args.device, ctranslate2.get_cuda_device_count() > 0, overflow) as device:
                            verifier = whisper_model(args.model, device)
                            check, _ = verifier.transcribe(cleaned, beam_size=beam, language=language,
                                task="transcribe" if contextual else "translate", word_timestamps=True,
                                condition_on_previous_text=False, clip_timestamps=[t for span in quiet for t in span])
                            remaining += matching_dialogue(segments, check, len(cleaned)/16000)
                            del check, verifier
                    suppress_unresolved(folder / "background.wav", [span for span in confirmed if contains_speech(span)])
        if not (segments or drafts):
            raise ValueError("No translatable speech detected.")
        gc.collect()
        print("Identifying speakers and timing lines…", flush=True)
        speech = mono(analysis_audio, "speech.f32") if analysis_audio != source else audio
        turns = identify_turns(speech)
        candidates = assign_lines(segments, turns)
        english_cues = assign_lines(drafts, turns)
        # Direct translation can hear a vocalization the native pass omitted, anywhere on the timeline.
        for extra in english_cues:
            overlap = sum(max(0, min(extra["end"], cue["end"]) - max(extra["start"], cue["start"]))
                          for cue in candidates)
            if overlap < 0.15 * (extra["end"] - extra["start"]):
                extra["english"] = True
                candidates.append(extra)
        duration = len(audio) / 16000
        lines = timed_lines(candidates, duration)
        if not lines:
            raise ValueError("No spoken lines to render.")
        print(f"Rendering {len(lines)} lines across {len({line['speaker'] for line in lines})} speaker group(s).", flush=True)
        if contextual and language != target:
            print("Translating the source transcript in context…", flush=True)
            script = folder / "lines.json"
            script.write_text(json.dumps(lines, ensure_ascii=False))
            translated = subprocess.run([worker, APP / "translate_lines.py", folder, language, target])
            if translated.returncode == 0:
                lines = json.loads(script.read_text())
            elif target != "en":
                raise ValueError(f"The translator could not complete the {output.name} dialogue.")
            else:
                # Whisper already produced a direct English pass with its own word timings.
                lines = timed_lines(english_cues, duration)
                if not lines:
                    raise ValueError("Neither translator produced an English dub.")
                print("Using the timed English translation from Whisper.", flush=True)
        if match:
            write_references(lines, turns, analysis_audio, folder)
        print(f"Synthesizing {output.name} voices…", flush=True)
        synthesize(lines, folder, match, target, args.lab_runtime)
        print("Mixing soundtrack…", flush=True)
        mix(lines, source, folder)
        print("Encoding video…", flush=True)
        render(media, lines, folder, target)
        metadata_file = os.getenv("REELDUB_METADATA_FILE")
        if metadata_file:
            path = Path(metadata_file)
            details = json.loads(path.read_text()) if path.is_file() else {}
            translator = folder / "translator.txt"
            details.update(source_language=language, first_caption=lines[0]["text"].strip()[:240],
                           asr_model=f"Whisper {args.model}",
                           translation_model=translator.read_text() if translator.exists() else (
                               "Same language" if language == target else f"Whisper {args.model}"),
                           voice_model=f"Qwen3-TTS-12Hz-{os.getenv('REELDUB_QWEN_SIZE', '1.7B')}-Base" if match else "Kokoro v1.0",
                           audio_mix_version=2)
            path.write_text(json.dumps(details, ensure_ascii=False))
        os.link(folder / "finished.mp4", dest)  # Publish atomically without overwriting another job.
        print(dest)


def main():
    parser = argparse.ArgumentParser(description="Translate and dub a video using local models.")
    parser.add_argument("input", nargs="?", help="Instagram Reel URL, or local video")
    parser.add_argument("-o", "--output", type=Path, help="output MP4 (default: output/<video>.<language>.mp4)")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    installed = APP / ".venv/reeldub-model"
    parser.add_argument("--model", choices=["small", "medium", "large-v3"],
                        default=installed.read_text().strip() if installed.exists() else "large-v3")
    parser.add_argument("--target-language", choices=list(LANGUAGES), default="en", help="output audio and subtitle language")
    parser.add_argument("--quality", choices=["best", "medium", "fast"], help="Use a Studio benchmark preset")
    parser.add_argument("--lab-separation", choices=["fp32", "fp16"], default="fp32")
    parser.add_argument("--lab-runtime", choices=["standard","omni","graph"], default="standard")
    parser.add_argument("--lab-voice", choices=["matched", "small", "preset"], help="Override the voice in a Studio preset")
    parser.add_argument("--voice-batch", type=int, choices=[1, 2], help="Override matched voices per batch in a Studio preset")
    parser.add_argument("--language", help="optional source language code, for example fa or es")
    studio = (APP / ".qwen-venv/ready").exists()
    parser.add_argument("--background", choices=["separate", "stereo"], default="separate" if studio else "stereo")
    parser.add_argument("--translation", choices=["context", "whisper"], default="context" if studio else "whisper")
    parser.add_argument("--voice-mode", choices=["auto", "preset", "match"], default="auto")
    try:
        run(parser.parse_args())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"ReelDub: {error}\n")


if __name__ == "__main__":
    main()
