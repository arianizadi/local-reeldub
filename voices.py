import json
import os
from itertools import groupby
from pathlib import Path
import subprocess
import sys

import numpy as np
import sherpa_onnx as so
import soundfile as sf

from server.languages import LANGUAGES
from server.upload_voice import KITTEN_ENGINE, KITTEN_MATCH, KITTEN_ROOT, VOICE_ERRORS
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


def write_references(lines, turns, source, folder, *, require_isolated=False):
    # Anchor the reference to the actual utterance. A single speaker label may span
    # distinct voices after an off-screen cut or a diarization mistake.
    bounds = []
    with sf.SoundFile(source) as stream:
        rate = stream.samplerate
        for i, line in enumerate(lines):
            left, right = line["start"], line["end"]
            own = [t for t in turns if t.speaker == line["speaker"] and t.start < right and t.end > left]
            if require_isolated and not own:
                raise ValueError(VOICE_ERRORS["unclear_speaker"])
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
            if require_isolated and not any(b - a >= 0.25 for a, b in spans):
                raise ValueError(VOICE_ERRORS["unclear_speaker"])
            left, right = max(spans or [(left, right)], key=lambda span: span[1] - span[0])
            stream.seek(min(len(stream), max(0, round(left * rate))))
            ref = stream.read(max(1, round(min(8, right - left) * rate)), always_2d=True, dtype="float32")
            bounds.append((left, left + len(ref) / rate))
            if len(ref) < rate // 4:
                # The speaker encoder needs a valid window even for an isolated interjection.
                ref = np.pad(ref, ((0, rate // 4 - len(ref)), (0, 0)))
            sf.write(folder / f"reference-{i}.wav", ref.mean(axis=1), rate)
    return bounds


def synthesize(lines, folder, match, target="en", runtime="standard", narrator_engine=None, match_engine=None):
    if match_engine is not None:
        if match_engine != KITTEN_MATCH or not match or target != "en" or narrator_engine is not None:
            raise ValueError("Unsupported speaker-matching engine.")
        from server.gpu import stage
        refs = [{"text": line["text"], "reference_text": line["reference_text"]} for line in lines]
        (folder / "lines.json").write_text(json.dumps(refs))
        root = Path(__file__).resolve().parent
        if os.getenv("REELDUB_KITTEN_MATCH_SPOOL"):
            command = [sys.executable, root / "deploy/fleet/match-runner.py"]
        elif os.getenv("REELDUB_SERVICE_LIMITS") or os.getenv("REELDUB_OFFLINE_ENGINE"):
            raise RuntimeError("The private Kitten speaker-matching service is unavailable.")
        else:
            command = [root / "kitten-match"]
        with stage("exclusive"):
            print("GPU stage: Voicing", flush=True)
            subprocess.run([*command, folder], check=True)
        return
    if narrator_engine is not None:
        if narrator_engine != KITTEN_ENGINE or match or target != "en":
            raise ValueError("Unsupported narrator selection.")
        from server.gpu import stage

        (folder / "lines.json").write_text(json.dumps([line["text"] for line in lines]))
        env = dict(os.environ, HF_HOME=str(KITTEN_ROOT / "hf"), HF_HUB_OFFLINE="1")
        with stage("exclusive"):
            print("GPU stage: Voicing", flush=True)
            subprocess.run([KITTEN_ROOT / "env/bin/python", Path(__file__).with_name("kitten_voice.py"), folder],
                           check=True, env=env)
        return
    if match:
        python = Path(__file__).resolve().parent / ".qwen-venv/bin/python"
        (folder / "lines.json").write_text(json.dumps(lines))
        if runtime == "standard":
            subprocess.run([python, Path(__file__).with_name("qwen_voice.py"), folder, target], check=True)
        else:
            root = Path(__file__).resolve().parent
            env = os.environ.copy()
            if runtime == "omni":
                python = root / ".omni-venv/bin/python"
                # vLLM ships a different PyTorch/CUDA build from the Studio environment.
                env["LD_LIBRARY_PATH"] = os.pathsep.join(
                    path for path in env.get("LD_LIBRARY_PATH", "").split(os.pathsep)
                    if path and not Path(path).is_relative_to(root / ".qwen-venv")
                )
            elif runtime == "graph":
                env["PYTHONPATH"] = str(root / ".tts-graph")
            else:
                raise ValueError("Unsupported lab voice runtime.")
            subprocess.run([python, root / "lab_voice.py", folder, target, runtime], env=env, check=True)
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
