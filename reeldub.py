import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import gc
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import urlparse

import ctranslate2
import numpy as np
from faster_whisper import WhisperModel

from server.progress import report
from server.gpu import stage, transcription_device
from index_translate import translated_tracks
from screen_ocr import detect_tracks
from server.languages import LANGUAGES
from server.upload_voice import (KITTEN_MATCH, KITTEN_MATCH_MODEL,
                                 VOICE_ERRORS, kitten_match_ready)
from server.source import CAPTION_FILTER_VERSION, SCREEN_TEXT_VERSION, inspect_video
from server.video_url import video_url
from server.download import download_reel
from clean_background import leakage_regions, matching_dialogue, merge_regions, suppress_unresolved
from render import ff, mix, render, source_input_options, source_time_limit, RATE
from voices import assign_lines, identify_turns, synthesize, write_references

APP = Path(__file__).resolve().parent

# Keep brief interjections and quiet speech while excluding gaps and non-speech
# intros. faster-whisper restores word times to the original video clock.
SPEECH_DECODE = dict(vad_filter=True, vad_parameters=dict(threshold=.35, min_speech_duration_ms=100,
                     min_silence_duration_ms=600, speech_pad_ms=220), hallucination_silence_threshold=1.0,
                     word_timestamps=True, condition_on_previous_text=False)


def subtitle_credit(text):
    """Recognize standalone subtitle bylines, including recurring Whisper training artifacts."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))
    # Treat Amara.org as a single token so the domain period isn't a sentence end.
    text = re.sub(r"amara\s*[.．]\s*org\b", "amaraorg", text)
    text = re.sub(r"^[\W_]+|[\W_]+$", "", text)
    if not text or len(text) > 160 or re.search(r"[!?。！？]|[.]\s+[a-z]", text):
        return False
    byline = re.match(r"^(?:subtitles?|captions?|closed captioning)\s+"
                      r"(?:(?:created|provided|made|translated|produced|prepared|contributed)\s+)?by\s+\S", text)
    if byline:
        attribution = text[byline.end()-1:]
        return len(attribution.split()) <= 9 and not re.search(
            r"\b(?:are|is|was|were|will|can|should|have|has)\b", attribution)
    if re.fullmatch(r"alt\s*yaz[ıi]\s*[:：-]?\s*m\s*[.]?\s*k", text):
        return True
    if "amaraorg" in text:
        credit = re.search(r"subtit|subtitr|sous.titr|sottotitol|untertitel|legend[aa]|alt\s*yaz|"
                           r"субтитр|زیرنویس|ترجمة|字幕|자막", text)
        # An actual discussion of subtitles or the service is still valid speech.
        return bool(credit and (credit.start() <= 4 or text.startswith(("amaraorg", "由amaraorg", "ترجمة"))))
    return False


def credit_clauses(text):
    return re.split(r"(?<=[.!?])\s+|(?<=[。！？])", text.strip())


def spoken_segments(segments):
    for segment in segments:
        clauses = credit_clauses(segment.text)
        if not any(subtitle_credit(c) for c in clauses):
            yield segment
            continue
        if all(not c or subtitle_credit(c) for c in clauses):
            continue
        groups, words = [], []
        for word in segment.words or []:
            words.append(word)
            if word.word.rstrip().endswith((".", "!", "?", "。", "！", "？")):
                groups.append(words)
                words = []
        if words:
            groups.append(words)
        for group in groups:
            text = "".join(w.word for w in group).strip()
            if text and not subtitle_credit(text):
                yield replace(segment, start=group[0].start, end=group[-1].end, words=group, text=text)
        if not groups:
            kept = " ".join(c for c in clauses if c and not subtitle_credit(c))
            if kept:
                yield replace(segment, text=kept)


def input_video(value, folder):
    hosted = bool(os.getenv("REELDUB_SERVICE_LIMITS"))
    if hosted and os.getenv("REELDUB_STAGED_INPUT") == value:
        return Path(value)
    if hosted or urlparse(value).scheme:
        value = video_url(value)
        return download_reel(value, folder, metadata=os.getenv("REELDUB_METADATA_FILE"))
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
    segments, info = model.transcribe(audio, beam_size=beam, language=language, task="transcribe" if native else "translate",
                                      **SPEECH_DECODE)
    result = list(spoken_segments(segments))
    print(f"Source language: {info.language}. Found {len(result)} segments.", flush=True)
    drafts = []
    if native and target == "en" and info.language != "en":
        translated, _ = model.transcribe(audio, beam_size=beam, language=info.language, task="translate", **SPEECH_DECODE)
        drafts = list(spoken_segments(translated))
    leaks = []
    if bed is not None:
        recognized, _ = model.transcribe(bed, beam_size=beam, language=info.language, task="transcribe" if native else "translate",
                                         **SPEECH_DECODE)
        leaks = matching_dialogue(result, recognized, len(bed) / 16000)
    return result, info.language, drafts, leaks


def transcribe_reference(model, speech_16k, span, language, beam):
    # Match the isolated reference window using the same decoded samples as diarization.
    left, right = (round(seconds * 16000) for seconds in span)
    heard, _ = model.transcribe(speech_16k[left:right], language=language, beam_size=beam,
                                condition_on_previous_text=False)
    text = "".join(segment.text for segment in heard if not subtitle_credit(segment.text)).strip()
    if not text:
        raise ValueError(VOICE_ERRORS["unclear_speaker"])
    return text


def timed_lines(candidates, duration):
    lines = []
    for cue in sorted(candidates, key=lambda item: item["start"]):
        start = max(0, cue["start"], lines[-1]["end"] if lines else 0)
        end = min(duration, cue["end"])
        if end > start and any(c.isalnum() for c in cue["text"]) and not subtitle_credit(cue["text"]):
            lines.append({**cue, "start": start, "end": end})
    return lines


def grounded_drafts(drafts, native_lines, turns):
    """Allow a translation-only cue when isolated speaker activity supports it."""
    accepted = []
    for cue in drafts:
        left, right = cue["start"], cue["end"]
        duration = right - left
        if duration <= 0:
            continue
        native_overlap = sum(max(0., min(right, line["end"]) - max(left, line["start"])) for line in native_lines)
        if native_overlap >= .15 * duration:
            accepted.append((cue, False))
            continue
        # Word times on a hallucinated credit can cover an entire musical intro
        # and brush the first real word. Count the union of speaker intervals.
        spans = sorted((max(left, t.start), min(right, t.end)) for t in turns if t.end > left and t.start < right)
        covered, last = 0., left
        for start, end in spans:
            covered += max(0., end - max(start, last))
            last = max(last, end)
        if covered >= .55 * duration:
            accepted.append((cue, True))
    return accepted


def has_audio_stream(media, remote):
    audio = subprocess.check_output(["ffprobe", "-v", "error", *source_input_options(remote),
                                     "-select_streams", "a:0", "-show_entries", "stream=index",
                                     "-of", "csv=p=0", media], timeout=30)
    return bool(audio.strip())


def translate_screen(tracks, language, target):
    if not tracks:
        return []
    print("Translating on-screen text…", flush=True)
    with stage("translation"):
        return translated_tracks(tracks, LANGUAGES[target].name, language)


def save_render(folder, dest, tracks, **metadata):
    if filename := os.getenv("REELDUB_METADATA_FILE"):
        path = Path(filename)
        details = json.loads(path.read_text()) if path.is_file() else {}
        engine = folder / "screen-ocr-engine.txt"
        if engine.is_file():
            details["screen_text_engine"] = engine.read_text().strip()[:120]
        details.update(metadata, screen_text_version=SCREEN_TEXT_VERSION, screen_text_count=len(tracks),
                       caption_filter_version=CAPTION_FILTER_VERSION)
        path.write_text(json.dumps(details, ensure_ascii=False))
    os.link(folder / "finished.mp4", dest)
    print(dest)


def text_only(media, folder, dest, language, target, remote):
    print("Reading on-screen text…", flush=True)
    regions = detect_tracks(media, folder, source_language=language, remote=remote)
    tracks = translate_screen(regions, language, target)
    if not tracks:
        raise ValueError("No translatable speech or on-screen text detected.")
    print("Encoding video…", flush=True)
    render(media, [], folder, target, remote=remote, screen_tracks=tracks)
    save_render(folder, dest, tracks, rendered_voice_mode="text_only",
                first_caption=tracks[0]["translation"][:240],
                translation_model="IndexTeam/Index-Translate-9B-Q5_K_M")


def run(args):
    value = args.input or input("Paste a public video link (or a video path): ").strip()
    target = args.target_language
    output = LANGUAGES[target]
    if not output.matched:
        raise ValueError(f"No installed speaker-matching engine supports {output.name} output.")
    if not (APP / ".qwen-venv/bin/python").is_file():
        raise ValueError("Install the Studio models with ./setup.sh --studio.")
    if args.match_engine and args.quality != "fast":
        raise ValueError("Only Fast mode uses Kitten.")
    args.match_engine = KITTEN_MATCH if args.quality == "fast" else None
    if args.match_engine and (target != "en" or not kitten_match_ready()):
        raise ValueError("Kitten speaker matching is unavailable for this output.")
    beam = 1 if args.match_engine else 5
    args.translation = "context"
    os.environ["REELDUB_QWEN_SIZE"] = "1.7B"
    os.environ["REELDUB_TTS_BATCH_SIZE"] = str(args.voice_batch or os.getenv("REELDUB_TTS_BATCH_SIZE") or 2)
    output_dir = args.output.expanduser().resolve().parent if args.output else Path.cwd() / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".reeldub-", dir=output_dir) as temp:
        folder = Path(temp)
        remote = urlparse(value).scheme in ("https", "http")
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
        if not has_audio_stream(media, remote):
            return text_only(media, folder, dest, args.language, target, remote)
        source = folder / "source.wav"
        ff(*source_input_options(remote), "-i", media, *source_time_limit(), "-map", "0:a:0", "-vn", "-ac", "2", "-ar", str(RATE),
           "-c:a", "pcm_s16le", "-rf64", "auto", source)
        # Muted uploads sometimes carry a full PCM track of digital silence.
        import soundfile as sf
        with sf.SoundFile(source) as samples:
            if not any(np.abs(block).max() > 1e-4 for block in samples.blocks(blocksize=65536)):
                return text_only(media, folder, dest, args.language, target, remote)
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
                suppress_unresolved(folder / "background.wav", regions)
        if not (segments or drafts):
            return text_only(media, folder, dest, language, target, remote)
        gc.collect()
        print("Identifying speakers and timing lines…", flush=True)
        speech = mono(analysis_audio, "speech.f32") if analysis_audio != source else audio
        turns = identify_turns(speech)
        if not turns:
            # Whisper can hallucinate dialogue over music. Only keep the original
            # soundtrack when there is neither a speaker turn nor confident speech.
            if all(getattr(s, "no_speech_prob", 0) >= 0.6 for s in [*segments, *drafts]):
                return text_only(media, folder, dest, language, target, remote)
            raise ValueError(VOICE_ERRORS["unclear_speaker"])
        candidates = assign_lines(segments, turns)
        corroborated = grounded_drafts(assign_lines(drafts, turns), candidates, turns)
        english_cues = [cue for cue, _ in corroborated]
        candidates.extend({**cue, "english": True} for cue, missing in corroborated if missing)
        duration = len(audio) / 16000
        lines = timed_lines(candidates, duration)
        if not lines:
            raise ValueError("No spoken lines to render.")
        print(f"Rendering {len(lines)} lines across {len({line['speaker'] for line in lines})} speaker group(s).", flush=True)
        if args.match_engine and language != target:
            print("Translating the source with Whisper…", flush=True)
            lines = timed_lines(english_cues, duration)
            if not lines:
                raise ValueError("Whisper could not produce an English translation for Fast.")
        elif contextual and language != target:
            print("Translating the source transcript in context…", flush=True)
            script = folder / "lines.json"
            script.write_text(json.dumps(lines, ensure_ascii=False))
            translation = [worker, APP / "translate_lines.py", folder, language, target, "--index-only"]
            translated = subprocess.run(translation)
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
        # A translator can independently invent a credit. Both captions and TTS
        # are generated from these final cues, so filter here before either output.
        for line in lines:
            clauses = credit_clauses(line["text"])
            if any(subtitle_credit(c) for c in clauses):
                line["text"] = " ".join(c for c in clauses if c and not subtitle_credit(c))
        lines = [line for line in lines if line["text"].strip()]
        if not lines:
            return text_only(media, folder, dest, language, target, remote)
        spans = write_references(lines, turns, analysis_audio, folder, require_isolated=True)
        if args.match_engine:
            words = [word for segment in segments for word in (segment.words or [])]
            missing = []
            for i, (line, (left, right)) in enumerate(zip(lines, spans, strict=True)):
                line["reference_text"] = "".join(word.word for word in words
                                                 if left <= (word.start + word.end) / 2 < right).strip()
                if not line["reference_text"]:
                    missing.append(i)
            if missing:
                # Some short cues lie between word midpoints in the full-video transcript.
                verifier = whisper_model(args.model, "cpu")
                for i in missing:
                    lines[i]["reference_text"] = transcribe_reference(verifier, speech, spans[i], language, beam)
                del verifier
        # OCR uses the CPU. Let it read the picture while the selected voice engine
        # owns the GPU, then acquire the translation slot for any detected passages.
        with ThreadPoolExecutor(max_workers=1) as reader:
            regions = reader.submit(detect_tracks, media, folder, source_language=language, remote=remote)
            print(f"Synthesizing {output.name} voices…", flush=True)
            synthesize(lines, folder, target, args.lab_runtime, args.match_engine)
            print("Mixing soundtrack…", flush=True)
            mix(lines, source, folder)
            print("Reading on-screen text…", flush=True)
            tracks = translate_screen(regions.result(), language, target)
        print("Encoding video…", flush=True)
        render(media, lines, folder, target, voice_label="Matched speaker", remote=remote, screen_tracks=tracks)
        translator = folder / "translator.txt"
        save_render(folder, dest, tracks, source_language=language, first_caption=lines[0]["text"].strip()[:240],
                    asr_model=f"Whisper {args.model}",
                    translation_model=translator.read_text() if translator.exists() else (
                        "Same language" if language == target else f"Whisper {args.model}"),
                    voice_model=KITTEN_MATCH_MODEL if args.match_engine else "Qwen3-TTS-12Hz-1.7B-Base",
                    rendered_voice_mode="match", audio_mix_version=2)


def main():
    parser = argparse.ArgumentParser(description="Translate and dub a video using local models.")
    parser.add_argument("input", nargs="?", help="Public video URL, or local video")
    parser.add_argument("-o", "--output", type=Path, help="output MP4 (default: output/<video>.<language>.mp4)")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    installed = APP / ".venv/reeldub-model"
    parser.add_argument("--model", choices=["small", "medium", "large-v3"],
                        default=installed.read_text().strip() if installed.exists() else "large-v3")
    parser.add_argument("--target-language", choices=[k for k,v in LANGUAGES.items() if v.matched], default="en", help="output audio and subtitle language")
    parser.add_argument("--quality", choices=["best", "fast"], default="best", help="Best uses Qwen; Fast tries Kitten then Best")
    parser.add_argument("--lab-separation", choices=["fp32", "fp16"], default="fp32")
    parser.add_argument("--lab-runtime", choices=["standard","omni","graph"], default="standard")
    parser.add_argument("--voice-batch", type=int, choices=[1, 2], help="Override matched voices per batch in a Studio preset")
    parser.add_argument("--language", help="optional source language code, for example fa or es")
    parser.add_argument("--background", choices=["separate", "stereo"], default="separate")
    parser.add_argument("--match-engine", choices=[KITTEN_MATCH], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.input is None:
        args.input = input("Paste a public video link (or a video path): ").strip()
    try:
        try:
            run(args)
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
            if args.quality != "fast" or os.getenv("REELDUB_SERVICE_LIMITS"):
                raise
            print("Fast could not finish this dub. Trying Best with Qwen.", file=sys.stderr)
            args.quality, args.match_engine = "best", None
            run(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"ReelDub: {error}\n")


if __name__ == "__main__":
    main()
