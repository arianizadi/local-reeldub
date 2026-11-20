import json
from itertools import groupby
from pathlib import Path
import subprocess

import numpy as np
import sherpa_onnx as so
import soundfile as sf

from server.languages import LANGUAGES
from server.progress import report
from assets import CACHE, SPEAKER


def identify_turns(audio):
    config = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(CACHE / "segmentation.onnx")),
            num_threads=2,
        ),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=str(CACHE / SPEAKER), num_threads=2),
        clustering=so.FastClusteringConfig(num_clusters=-1, threshold=0.5),
        min_duration_on=0.25, min_duration_off=0.35,
    )
    if not config.validate():
        raise RuntimeError("Missing diarization model. Run ./setup.sh.")
    result = so.OfflineSpeakerDiarization(config).process(audio)
    return result.sort_by_start_time()


def speaker_at(start, end, turns):
    def score(turn):
        overlap = max(0, min(end, turn.end) - max(start, turn.start))
        distance = max(0, turn.start - end, start - turn.end)
        return overlap, -distance

    return max(turns, key=score).speaker if turns else 0


def assign_lines(segments, turns):
    # Word alignment lets one Whisper segment contain several conversational turns.
    lines = []
    for seg in segments:
        words = seg.words or []
        if not words:
            lines.append(dict(start=seg.start, end=seg.end, text=seg.text.strip(),
                              speaker=speaker_at(seg.start, seg.end, turns)))
            continue
        for speaker, run in groupby(words, key=lambda w: speaker_at(w.start, w.end, turns)):
            group = list(run)
            start = group[0].start
            first_end = group[0].end
            if first_end - start > 1:
                # Whisper may assign preceding silence to the first word after a long pause.
                nearby = [t.start for t in turns if start < t.start <= first_end + 0.15]
                if nearby:
                    start = min(max(nearby), first_end)
            lines.append(dict(start=start, end=group[-1].end,
                              text="".join(w.word for w in group).strip(), speaker=speaker))
    return lines


def write_references(lines, turns, source, folder):
    # Anchor the reference to the actual utterance. A single speaker label may span
    # distinct voices after an off-screen cut or a diarization mistake.
    with sf.SoundFile(source) as stream:
        rate = stream.samplerate
        for i, line in enumerate(lines):
            left, right = line["start"], line["end"]
            own = [t for t in turns if t.speaker == line["speaker"] and t.start < right and t.end > left]
            if own:
                best = max(own, key=lambda t: min(t.end, right) - max(t.start, left))
                # Very short words benefit from adjacent speech within the same turn.
                if right - left < 1.5:
                    left = max(best.start, min(left, best.end - 3))
                    right = min(best.end, max(right, left + 3))
                else:
                    left, right = max(best.start, left), min(best.end, right)
            spans = [(left, right)]
            for t in turns:
                if t.speaker != line["speaker"] and t.start < right and t.end > left:
                    remaining = []
                    for a, b in spans:
                        if a < t.start:
                            remaining.append((a, min(b, t.start)))
                        if b > t.end:
                            remaining.append((max(a, t.end), b))
                    spans = remaining
            left, right = max(spans or [(left, right)], key=lambda span: span[1] - span[0])
            stream.seek(min(len(stream), max(0, round(left * rate))))
            ref = stream.read(max(1, round(min(8, right - left) * rate)), always_2d=True, dtype="float32")
            if len(ref) < rate // 4:
                # The speaker encoder needs a valid window even for an isolated interjection.
                ref = np.pad(ref, ((0, rate // 4 - len(ref)), (0, 0)))
            sf.write(folder / f"reference-{i}.wav", ref.mean(axis=1), rate)


def synthesize(lines, folder, match, target="en"):
    if match:
        python = Path(__file__).resolve().parent / ".qwen-venv/bin/python"
        (folder / "lines.json").write_text(json.dumps(lines))
        subprocess.run([python, Path(__file__).with_name("qwen_voice.py"), folder, target], check=True)
    else:
        from kokoro_onnx import Kokoro

        model = Kokoro(str(CACHE / "kokoro-v1.0.fp16.onnx"), str(CACHE / "voices-v1.0.bin"))
        voices = LANGUAGES[target].voices
        assigned = {}
        for i, line in enumerate(lines):
            voice = assigned.setdefault(line["speaker"], voices[len(assigned) % len(voices)])
            samples, rate = model.create(line["text"], voice=voice, lang=LANGUAGES[target].preset_lang, speed=1.0)
            sf.write(folder / f"{i}.wav", samples, rate)
            report(completed=i+1, total=len(lines))
