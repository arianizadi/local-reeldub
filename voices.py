import json
from itertools import groupby
from pathlib import Path
import subprocess

import numpy as np
import sherpa_onnx as so
import soundfile as sf

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
            lines.append(dict(start=group[0].start, end=group[-1].end,
                              text="".join(w.word for w in group).strip(), speaker=speaker))
    return lines


def write_references(lines, turns, source, folder):
    # Split at overlaps so another speaker does not enter the conditioning clip.
    for speaker in {line["speaker"] for line in lines}:
        choices = []
        for turn in turns:
            if turn.speaker != speaker:
                continue
            cursor = turn.start
            for other in turns:
                if other.speaker == speaker or other.end <= cursor or other.start >= turn.end:
                    continue
                if other.start > cursor:
                    choices.append((cursor, other.start))
                cursor = max(cursor, other.end)
            if cursor < turn.end:
                choices.append((cursor, turn.end))
        if not choices:
            choices = [(line["start"], line["end"]) for line in lines if line["speaker"] == speaker]
        # Favor a single clean phrase. Collect a few shorter phrases if necessary.
        pieces = []
        total = 0
        with sf.SoundFile(source) as stream:
            rate = stream.samplerate
            for start, end in sorted(choices, key=lambda t: t[1] - t[0], reverse=True):
                count = min(int((end - start) * rate), 8 * rate - total)
                if count < 0.12 * rate:
                    continue
                stream.seek(min(len(stream), max(0, round(start * rate))))
                part = stream.read(count, always_2d=True, dtype="float32").mean(axis=1)
                if len(part):
                    pieces.append(part)
                    total += len(part)
                if total >= 3 * rate:
                    break
        if not pieces:
            raise RuntimeError(f"Too little audible speech to sample speaker {speaker}. Try --voice-mode preset.")
        sf.write(folder / f"reference-{speaker}.wav", np.concatenate(pieces), rate)


def synthesize(lines, folder, match):
    if match:
        python = Path(__file__).resolve().parent / ".qwen-venv/bin/python"
        (folder / "lines.json").write_text(json.dumps(lines))
        subprocess.run([python, Path(__file__).with_name("qwen_voice.py"), folder], check=True)
    else:
        from kokoro_onnx import Kokoro

        model = Kokoro(str(CACHE / "kokoro-v1.0.fp16.onnx"), str(CACHE / "voices-v1.0.bin"))
        voices = ["af_heart", "am_fenrir", "bf_emma", "bm_george", "af_bella", "am_michael"]
        assigned = {}
        for i, line in enumerate(lines):
            voice = assigned.setdefault(line["speaker"], voices[len(assigned) % len(voices)])
            samples, rate = model.create(line["text"], voice=voice, lang="en-us", speed=1.0)
            sf.write(folder / f"{i}.wav", samples, rate)
